#!/usr/bin/env python3
"""tools/audio/tts_finetune/train.py's loop on a toy model: a run stopped and resumed ends exactly
where an uninterrupted one does, whatever step it stopped at, with an epoch whose micro-batches
do not divide into accumulation groups. Needs torch and accelerate; skipped without them (Studio's
Python has both: ~/.unsloth/studio/unsloth_studio/bin/python tests/tools/tts_finetune_test.py)."""

import argparse
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
