#!/usr/bin/env python3
"""tools/audio/tts_finetune/train.py's loop on a toy model: a run stopped and resumed ends exactly
where an uninterrupted one does, whatever step it stopped at, with an epoch whose micro-batches
do not divide into accumulation groups. Needs torch and accelerate; skipped without them (Studio's
Python has both: ~/.unsloth/studio/unsloth_studio/bin/python tests/tools/tts_finetune_test.py)."""

import argparse
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import torch
    from accelerate import Accelerator
    SPEC = importlib.util.spec_from_file_location(
        "tts_train", Path(__file__).parents[2] / "tools/audio/tts_finetune/train.py")
    train = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = train
    SPEC.loader.exec_module(train)
except ImportError as error:  # pragma: no cover
    train = None
    SKIP = str(error)

ROWS = 5            # five micro-batches of one row: a group of four, then a group of one
EPOCHS = 2
ACCUM = 4


def args(**changes):
    values = dict(batch_size=1, grad_accum=ACCUM, lr=0.05, warmup_steps=0, epochs=EPOCHS, max_steps=0, seed=11,
                  log_every=1, checkpoint_minutes=1e9, keep=2)
    values.update(changes)
    return argparse.Namespace(**values)


class Toy:
    """Rows 0..4 of a 3-feature regression; step_fn records the row of every micro-batch."""

    def __init__(self):
        generator = torch.Generator().manual_seed(3)
        self.x = torch.randn(ROWS, 3, generator=generator)
        self.y = torch.randn(ROWS, 1, generator=generator)
        self.seen = []

    def __len__(self):
        return ROWS

    def __getitem__(self, i):
        return {"id": torch.tensor([i]), "x": self.x[i], "y": self.y[i]}

    @staticmethod
    def collate(items):
        return {k: torch.stack([item[k] for item in items]) for k in items[0]}

    def step(self, model, batch, on_micro=None):
        self.seen.extend(batch["id"].flatten().tolist())
        if on_micro:
            on_micro(len(self.seen))
        loss = torch.nn.functional.mse_loss(model(batch["x"]), batch["y"])
        return loss, torch.ones(1, 4)


def build(out: Path, resume=None, **changes):
    """A fresh model, optimizer and accelerator, as a new process would have; resumed if asked."""
    torch.manual_seed(0)
    accelerator = Accelerator(cpu=True, mixed_precision="no")
    model = torch.nn.Linear(3, 1)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.05, weight_decay=0.01)
    model, optimizer = accelerator.prepare(model, optimizer)
    progress = {"epoch": 0, "step_in_epoch": 0, "global_step": 0, "samples_seen": 0, "train_seconds": 0.0}
    speaker = None
    if resume is not None:
        accelerator.load_state(str(resume))
        progress.update(__import__("json").loads((resume / "trainer.json").read_text()))
        speaker = torch.load(resume / "speaker.pt")
    return accelerator, model, optimizer, progress, speaker


def go(out: Path, toy: Toy, on_micro=None, resume=None, **changes):
    options = args(**changes)
    accelerator, model, optimizer, progress, speaker = build(out, resume)
    run = train.Run(out, options)
    outcome, speaker = train.train_loop(accelerator, model, optimizer, toy, toy.collate,
                                        lambda m, b: toy.step(m, b, on_micro), options, run, progress, speaker)
    weights = {k: v.detach().clone() for k, v in accelerator.unwrap_model(model).state_dict().items()}
    return outcome, weights, progress, run


@unittest.skipIf(train is None, "torch and accelerate are not installed")
class ResumeTest(unittest.TestCase):
    def reference(self):
        toy = Toy()
        with tempfile.TemporaryDirectory() as tmp:
            outcome, weights, progress, _ = go(Path(tmp), toy)
        self.assertEqual((outcome, progress["global_step"], progress["epoch"]), ("done", 4, 2))
        self.assertEqual(sorted(toy.seen[:ROWS]), list(range(ROWS)))     # each epoch shows every row once
        self.assertEqual(sorted(toy.seen[ROWS:]), list(range(ROWS)))
        self.assertNotEqual(toy.seen[:ROWS], toy.seen[ROWS:])            # and shuffles differently
        return toy.seen, weights

    def test_stopping_after_any_step_and_resuming_changes_nothing(self):
        order, final = self.reference()
        for stop_after in (1, 2, 3):  # inside epoch 0, at its end (the group of one), inside epoch 1
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                toy = Toy()
                outcome, _, progress, _ = go(out, toy, max_steps=stop_after)
                self.assertEqual((outcome, progress["global_step"]), ("stopped", stop_after))
                latest = out / "ckpt" / (out / "ckpt" / "LATEST").read_text()
                outcome, weights, progress, _ = go(out, toy, resume=latest)
                self.assertEqual((outcome, progress["global_step"], progress["epoch"]), ("done", 4, 2))
                self.assertEqual(toy.seen, order)            # no micro-batch repeated or lost
                for name, value in final.items():
                    self.assertTrue(torch.allclose(value, weights[name], atol=0, rtol=0), name)

    def test_a_stop_request_waits_for_the_group_to_finish(self):
        order, final = self.reference()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            toy = Toy()
            # Asked for while the second micro-batch of the first group runs: the step still completes.
            outcome, _, progress, run = go(out, toy, on_micro=lambda n: (out / "STOP").write_text("x") if n == 2 else None)
            self.assertEqual((outcome, progress["global_step"], progress["step_in_epoch"]), ("stopped", 1, 1))
            self.assertEqual(len(toy.seen), ACCUM)
            latest = out / "ckpt" / (out / "ckpt" / "LATEST").read_text()
            (out / "STOP").unlink()
            outcome, weights, progress, _ = go(out, toy, resume=latest)
            self.assertEqual(outcome, "done")
            self.assertEqual(toy.seen, order)
            for name, value in final.items():
                self.assertTrue(torch.allclose(value, weights[name], atol=0, rtol=0), name)

    def test_the_last_group_of_an_epoch_is_scaled_by_its_own_size(self):
        # One epoch of five rows with batch 1: the group of one must not be divided by four.
        with tempfile.TemporaryDirectory() as tmp:
            toy = Toy()
            _, weights, _, _ = go(Path(tmp), toy, epochs=1)
        accelerator, model, optimizer, _, _ = build(Path("."), None)
        order = toy.seen
        for group in (order[:4], order[4:]):
            for row in group:
                loss = torch.nn.functional.mse_loss(model(toy.x[row:row + 1]), toy.y[row:row + 1])
                (loss / len(group)).backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            optimizer.zero_grad()
        for name, value in model.state_dict().items():
            self.assertTrue(torch.allclose(value, weights[name], atol=1e-7), name)

    def test_the_progress_files_say_what_happened(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            go(out, Toy())
            lines = [__import__("json").loads(l) for l in (out / "progress.jsonl").read_text().splitlines()]
            self.assertEqual([l["global_step"] for l in lines], [1, 2, 3, 4])
            self.assertEqual([l["step"] for l in lines], [1, 2, 1, 2])
            state = __import__("json").loads((out / "state.json").read_text())
            self.assertEqual((state["total_steps"], state["steps_per_epoch"], state["global_step"]), (4, 2, 4))


try:
    BUILD = importlib.util.spec_from_file_location(
        "tts_build", Path(__file__).parents[2] / "tools/audio/tts_finetune/build_dataset.py")
    dataset = importlib.util.module_from_spec(BUILD)
    BUILD.loader.exec_module(dataset)
except ImportError:  # pragma: no cover
    dataset = None


@unittest.skipIf(dataset is None, "pyarrow, soundfile and scipy are not installed")
class DatasetFilterTest(unittest.TestCase):
    def verdict(self, text, seconds):
        return dataset.keep_clip(text, seconds, 2.0, 15.0, 7.0, 21.0, r"[\u0400-\u04FF]")

    def test_a_transcript_that_cannot_belong_to_the_audio_is_dropped(self):
        sentence = "Всичко е наред, помисли се той."  # 31 characters
        self.assertIsNone(self.verdict(sentence, 2.5))
        self.assertEqual(self.verdict(sentence, 12.0), "chars per second")  # text far too short for it
        self.assertEqual(self.verdict(sentence * 4, 3.0), "chars per second")  # text far too long for it

    def test_other_scripts_and_bad_durations_are_dropped(self):
        self.assertEqual(self.verdict("This is an English sentence read aloud.", 3.0), "script")
        self.assertEqual(self.verdict("Всичко е наред.", 1.0), "duration")
        self.assertEqual(self.verdict("  ", 3.0), "empty text")

    def test_resampling_keeps_the_duration(self):
        import numpy
        samples = numpy.zeros(44100, dtype=numpy.float32)
        self.assertEqual(len(dataset.to_rate(samples, 44100)), dataset.RATE)


try:
    LAYOUT = importlib.util.spec_from_file_location(
        "tts_layout", Path(__file__).parents[2] / "tools/audio/tts_finetune/language_layout.py")
    layout = importlib.util.module_from_spec(LAYOUT)
    LAYOUT.loader.exec_module(layout)
except ImportError:  # pragma: no cover
    layout = None

# Token IDs of the real talker config; the values only have to differ from each other.
TALKER = argparse.Namespace(codec_think_id=2154, codec_nothink_id=2155, codec_think_bos_id=2156,
                            codec_think_eos_id=2157, codec_pad_id=2148, codec_bos_id=2149, codec_eos_token_id=2150)
CONFIG = argparse.Namespace(talker_config=TALKER, tts_pad_token_id=151671, tts_bos_token_id=151672,
                            tts_eos_token_id=151673)
BULGARIAN = 2100


def example(text_len, codes, language_id=None):
    """role tokens 11 12 13, then text_len - 3 text tokens (the upstream script cuts 5 off the end)."""
    ids = torch.tensor([[11, 12, 13] + [100 + n for n in range(text_len - 3)]])
    audio = torch.arange(1, codes + 1).unsqueeze(1).repeat(1, 16)
    return {"text_ids": ids, "audio_codes": audio, "ref_mel": torch.zeros(1, 4, 8), "language_id": language_id}


@unittest.skipIf(layout is None, "torch is not installed")
class LanguageLayoutTest(unittest.TestCase):
    def codec(self, batch, row):
        return batch["input_ids"][row, :, 1].tolist()

    def test_an_untagged_row_is_the_auto_prefix_the_server_builds(self):
        batch = layout.collate([example(6, 3)], CONFIG)
        # [nothink, think_bos, think_eos, speaker, pad] from position 3, then pads under the text, bos, codes, eos
        self.assertEqual(self.codec(batch, 0)[3:8], [2155, 2156, 2157, 0, 2148])
        self.assertEqual(batch["speaker_index"].tolist(), [6])
        self.assertEqual(batch["input_ids"][0, 7, 0].item(), CONFIG.tts_bos_token_id)

    def test_a_tagged_row_is_the_explicit_prefix_the_server_builds_and_everything_after_it_moves_by_one(self):
        plain = layout.collate([example(6, 3)], CONFIG)
        tagged = layout.collate([example(6, 3, BULGARIAN)], CONFIG)
        # [think, think_bos, language_id, think_eos, speaker, pad]
        self.assertEqual(self.codec(tagged, 0)[3:9], [2154, 2156, BULGARIAN, 2157, 0, 2148])
        self.assertEqual(tagged["speaker_index"].tolist(), [7])
        self.assertFalse(tagged["codec_embedding_mask"][0, 7].item())
        self.assertTrue(tagged["codec_embedding_mask"][0, 6].item())
        self.assertEqual(tagged["input_ids"][0, 8, 0].item(), CONFIG.tts_bos_token_id)
        # From the speaker slot on, a tagged row is the untagged one shifted right by one: the text, bos,
        # codes and eos, the labels, the code rows and the masks.
        for name in ("input_ids", "codec_0_labels", "codec_ids", "codec_mask"):
            before, after = plain[name][0], tagged[name][0]
            self.assertEqual(after[8:].tolist()[: before[7:].shape[0] - 1], before[7:-1].tolist(), name)
        self.assertEqual(tagged["codec_mask"][0].sum().item(), plain["codec_mask"][0].sum().item())
        self.assertEqual(tagged["attention_mask"][0].sum().item(), plain["attention_mask"][0].sum().item() + 1)

    def test_tagged_and_untagged_rows_share_a_batch_without_disturbing_each_other(self):
        alone = [layout.collate([example(7, 4)], CONFIG), layout.collate([example(6, 3, BULGARIAN)], CONFIG)]
        both = layout.collate([example(7, 4), example(6, 3, BULGARIAN)], CONFIG)
        self.assertEqual(both["speaker_index"].tolist(), [6, 7])
        for row, single in enumerate(alone):
            length = int(single["attention_mask"][0].sum())
            for name in ("input_ids", "codec_0_labels", "codec_ids"):
                self.assertEqual(both[name][row, :length].tolist(), single[name][0, :length].tolist(), (row, name))

    def test_without_tags_it_matches_the_upstream_collate(self):
        # Needs the upstream checkout setup.sh makes and qwen-tts: QWEN3_TTS_FINETUNING_DIR=.../finetuning
        directory = os.environ.get("QWEN3_TTS_FINETUNING_DIR")
        if not directory:
            self.skipTest("QWEN3_TTS_FINETUNING_DIR is not set")
        sys.path.insert(0, directory)
        try:
            from dataset import TTSDataset
        except ImportError as error:
            self.skipTest(f"upstream dataset.py does not import: {error}")
        upstream = TTSDataset.__new__(TTSDataset)
        upstream.config, upstream.lag_num = CONFIG, -1
        rows = [example(9, 5), example(6, 3)]
        expected = upstream.collate_fn([{k: v for k, v in r.items() if k != "language_id"} for r in rows])
        actual = layout.collate(rows, CONFIG)
        for name, value in expected.items():
            self.assertTrue(torch.equal(value, actual[name]), name)

    def test_which_rows_are_tagged_depends_only_on_the_row_and_the_seed(self):
        rows = [{"language": "Bulgarian"}] * 400 + [{"language": "klingon"}, {}]
        ids = {"bulgarian": BULGARIAN}
        first = layout.tagged_rows(rows, ids, 0.5, 7)
        self.assertEqual(first, layout.tagged_rows(rows, ids, 0.5, 7))
        self.assertNotEqual(first, layout.tagged_rows(rows, ids, 0.5, 8))
        self.assertTrue(150 < sum(tag == BULGARIAN for tag in first[:400]) < 250)
        self.assertEqual(first[400:], [None, None], "a language without an ID, or none, is never tagged")
        self.assertEqual(layout.tagged_rows(rows, ids, 0.0, 7), [None] * 402)
        # Changing one row's language leaves the others' choices alone.
        changed = layout.tagged_rows([{"language": "klingon"}] + rows[1:], ids, 0.5, 7)
        self.assertEqual(changed[1:], first[1:])


if __name__ == "__main__":
    unittest.main()
