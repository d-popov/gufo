#!/usr/bin/env python3
"""Run with python3 tests/tools/asr_finetune_test.py. The ASR fine-tuning helpers: the error rates a model is
judged by, and the reference text a model is trained on."""

import importlib.util
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2] / "tools/audio/asr_finetune"


def load(name):
    spec = importlib.util.spec_from_file_location(f"asr_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wer = load("wer")
export = load("export")
try:
    import numpy
    import soundfile
    prep = load("prepare_data")
except ImportError:  # pragma: no cover
    prep = None


class ErrorRateTest(unittest.TestCase):
    def test_casing_punctuation_and_the_language_prefix_do_not_count(self):
        rates = wer.error_rates([("language Bulgarian<asr_text>Здравей, свят!", "здравей свят")])
        self.assertEqual((rates["wer"], rates["cer"]), (0.0, 0.0))

    def test_errors_are_summed_over_the_corpus_not_averaged_per_clip(self):
        # One wrong word of 2, and one right word of 8: 1 error in 10 words, not 25% of two clips.
        pairs = [("a b", "a x"), ("c d e f g h i j", "c d e f g h i j")]
        self.assertAlmostEqual(wer.error_rates(pairs)["wer"], 0.1)

    def test_a_word_in_another_spelling_is_a_whole_word_error_but_a_small_character_error(self):
        # Macedonian spelling of a Bulgarian sentence: the case this project measures.
        rates = wer.error_rates([("тя не отговори", "таа не одговори")])
        self.assertAlmostEqual(rates["wer"], 2 / 3)
        self.assertLess(rates["cer"], 0.3)

    def test_insertions_and_an_empty_hypothesis_are_counted(self):
        self.assertEqual(wer.error_rates([("a b", "a b c d")])["wer"], 1.0)
        self.assertEqual(wer.error_rates([("a b", "")])["wer"], 1.0)


class ExportTest(unittest.TestCase):
    INDEX = {"thinker.model.embed_tokens.weight": "model-00001-of-00002.safetensors",
             "thinker.model.layers.0.w": "model-00001-of-00002.safetensors",
             "thinker.audio_tower.w": "model-00002-of-00002.safetensors",
             "thinker.lm_head.weight": "model-00002-of-00002.safetensors"}

    def test_tensors_go_to_the_shards_the_base_models_index_names_and_a_tied_head_counts_as_present(self):
        names = [n for n in self.INDEX if n != "thinker.lm_head.weight"]  # the trainer saves tied weights once
        shards = export.split_by_index(names, self.INDEX)
        self.assertEqual(sorted(shards["model-00001-of-00002.safetensors"]),
                         ["thinker.model.embed_tokens.weight", "thinker.model.layers.0.w"])
        self.assertEqual(sorted(shards["model-00002-of-00002.safetensors"]), ["thinker.audio_tower.w", "thinker.lm_head.weight"])

    def test_a_checkpoint_that_does_not_match_the_base_model_is_refused(self):
        names = [n for n in self.INDEX if n != "thinker.lm_head.weight"]
        with self.assertRaises(SystemExit):
            export.split_by_index(names[:-1], self.INDEX)                    # a tensor missing
        with self.assertRaises(SystemExit):
            export.split_by_index(names + ["thinker.surprise"], self.INDEX)  # one the base does not have
        with self.assertRaises(SystemExit):
            export.split_by_index([n for n in names if "embed" not in n], self.INDEX)  # no embedding to tie the head to


@unittest.skipIf(prep is None, "numpy and soundfile are not installed")
class PrepareTest(unittest.TestCase):
    def wav(self, seconds, rate):
        buffer = io.BytesIO()
        soundfile.write(buffer, numpy.zeros(int(seconds * rate), dtype="float32"), rate, format="WAV")
        return buffer.getvalue()

    def test_clips_become_16k_mono_and_keep_their_casing(self):
        samples = prep.to_16k(numpy.zeros((44100, 2), dtype="float32"), 44100)
        self.assertEqual(samples.shape, (16000,))

    def test_fleurs_rows_pair_each_clip_with_its_own_raw_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audio").mkdir()
            # id, file, raw, normalized, chars, samples, gender
            rows = [["1", "a.wav", "Здравей, свят!", "здравей свят", "з д", "16000", "MALE"],
                    ["2", "b.wav", "Друго.", "друго", "д р", "16000", "FEMALE"]]
            (root / "train.tsv").write_text("\n".join("\t".join(r) for r in rows), encoding="utf-8")
            with tarfile.open(root / "audio" / "train.tar.gz", "w:gz") as tar:
                for name in ("b.wav", "a.wav"):  # the tarball's order is not the tsv's
                    data = self.wav(1, 16000 if name == "a.wav" else 8000)
                    info = tarfile.TarInfo(f"train/{name}")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            got = {name: text for name, text, _ in prep.fleurs_rows(root, "train")}
            self.assertEqual(got, {"a.wav": "Здравей, свят!", "b.wav": "Друго."})


if __name__ == "__main__":
    unittest.main()
