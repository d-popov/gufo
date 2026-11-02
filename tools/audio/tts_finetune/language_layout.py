"""Training sequences with and without a language ID, laid out the way `gufo serve tts` builds them.

Qwen3-TTS prompts its talker with a codec prefix, and the server (src/models/qwen3_tts/hip/talker_runtime.cpp)
builds one of two:

    language auto:      [nothink, think_bos, think_eos,           speaker, pad, bos]
    language named:     [think,   think_bos, language_id, think_eos, speaker, pad, bos]

The upstream training script (QwenLM/Qwen3-TTS finetuning/dataset.py) only ever builds the first, with
hard-coded positions. A row trained with a language ID must use the longer prefix end to end: the text
channel gets one more pad, and the text, `bos`, the codes, the masks, the labels and the slot that holds the
speaker embedding all move one position later. `collate` does that per row, so a batch may mix both. With no
language tagged it produces exactly what the upstream collate does.
"""

import random

import torch

ROLE_TOKENS = 3        # <|im_start|> assistant \n: the same in every row
SPEAKER_SLOT = 6       # position of the speaker embedding in an untagged row; a tagged row's is one later


def tagged_rows(rows, language_ids: dict, fraction: float, seed: int) -> list:
    """For every row the language ID it trains with, or None. A row is tagged when its `language` is one
    the model has an ID for, with probability `fraction`; the choice depends on the row and the seed only,
    so a resumed run sees the same rows tagged."""
    rng = random.Random(seed)
    tags = []
    for row in rows:
        draw = rng.random()  # drawn for every row, so one row's language does not shift another's choice
        language = str(row.get("language") or "").strip().lower()
        tags.append(language_ids[language] if language in language_ids and draw < fraction else None)
    return tags


def collate(batch: list, config) -> dict:
    """The upstream collate with the language-tagged layout for rows that carry `language_id`.
    Rows are {"text_ids": [1, t], "audio_codes": [c, 16], "ref_mel": ..., "language_id": int | None}."""
    talker = config.talker_config
    extra = [1 if row.get("language_id") is not None else 0 for row in batch]
    lengths = [row["text_ids"].shape[1] + row["audio_codes"].shape[0] + e for row, e in zip(batch, extra)]
    b, t = len(batch), max(lengths) + 8

    input_ids = torch.zeros((b, t, 2), dtype=torch.long)
    codec_ids = torch.zeros((b, t, 16), dtype=torch.long)
    text_embedding_mask = torch.zeros((b, t), dtype=torch.bool)
    codec_embedding_mask = torch.zeros((b, t), dtype=torch.bool)
    codec_mask = torch.zeros((b, t), dtype=torch.bool)
    attention_mask = torch.zeros((b, t), dtype=torch.long)
    codec_0_labels = torch.full((b, t), -100, dtype=torch.long)
    speaker_index = torch.zeros(b, dtype=torch.long)

    for i, (row, e) in enumerate(zip(batch, extra)):
        text_ids = row["text_ids"]
        first_codec = row["audio_codes"][:, 0]
        tl, cl = text_ids.shape[1], first_codec.shape[0]
        start = 8 + e  # where the text begins (after tts_bos)

        # text channel
        input_ids[i, :ROLE_TOKENS, 0] = text_ids[0, :ROLE_TOKENS]
        input_ids[i, ROLE_TOKENS:start - 1, 0] = config.tts_pad_token_id
        input_ids[i, start - 1, 0] = config.tts_bos_token_id
        input_ids[i, start:start + tl - 3, 0] = text_ids[0, 3:]
        input_ids[i, start + tl - 3, 0] = config.tts_eos_token_id
        input_ids[i, start + tl - 2:start + tl + cl, 0] = config.tts_pad_token_id
        text_embedding_mask[i, :start + tl + cl] = True

        # codec channel: the prefix, a slot for the speaker embedding, pad; then pads under the text,
        # bos, the first code of every frame, eos
        if e:
            prefix = [talker.codec_think_id, talker.codec_think_bos_id, row["language_id"], talker.codec_think_eos_id]
        else:
            prefix = [talker.codec_nothink_id, talker.codec_think_bos_id, talker.codec_think_eos_id]
        prefix += [0, talker.codec_pad_id]  # 0: the speaker embedding goes here
        input_ids[i, ROLE_TOKENS:ROLE_TOKENS + len(prefix), 1] = torch.tensor(prefix)
        input_ids[i, start:start + tl - 3, 1] = talker.codec_pad_id
        input_ids[i, start + tl - 3, 1] = talker.codec_pad_id
        input_ids[i, start + tl - 2, 1] = talker.codec_bos_id
        input_ids[i, start + tl - 1:start + tl - 1 + cl, 1] = first_codec
        input_ids[i, start + tl - 1 + cl, 1] = talker.codec_eos_token_id

        codec_0_labels[i, start + tl - 1:start + tl - 1 + cl] = first_codec
        codec_0_labels[i, start + tl - 1 + cl] = talker.codec_eos_token_id
        codec_ids[i, start + tl - 1:start + tl - 1 + cl, :] = row["audio_codes"]

        speaker_index[i] = SPEAKER_SLOT + e
        codec_embedding_mask[i, ROLE_TOKENS:start + tl + cl] = True
        codec_embedding_mask[i, speaker_index[i]] = False  # the speaker embedding is added by the model step
        codec_mask[i, start + tl - 1:start + tl - 1 + cl] = True
        attention_mask[i, :start + tl + cl] = True

    return {
        "input_ids": input_ids,
        "ref_mels": torch.cat([row["ref_mel"] for row in batch], dim=0),
        "attention_mask": attention_mask,
        "text_embedding_mask": text_embedding_mask.unsqueeze(-1),
        "codec_embedding_mask": codec_embedding_mask.unsqueeze(-1),
        "codec_0_labels": codec_0_labels,
        "codec_ids": codec_ids,
        "codec_mask": codec_mask,
        "speaker_index": speaker_index,
    }
