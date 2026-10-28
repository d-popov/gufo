"""Token prices: integer micro-USD per 1M tokens, per served model with a default.

`PriceList` holds the prices file and the live `Pricing`: the admin replaces the whole
table (`save`), which rewrites the file atomically and then swaps the object, so a
request is billed with either the old table or the new one, never a mix.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unicodedata
from pathlib import Path

PRICE_FIELDS = ("input", "cached_input", "output")
# Defaults match OrcaRouter qwen/qwen3.8-flash (0.15 / 0.47 per 1M) plus 50%; cached
# input is 10% of input.
DEFAULT_PRICES = {"input": 225_000, "cached_input": 22_500, "output": 705_000}
MAX_MODELS = 64
MAX_MODEL_NAME = 128
MAX_PRICE = 10 ** 12  # $1M per 1M tokens; keeps every value exact in JavaScript too


class PricingError(ValueError):
    pass


def _prices(value, where: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise PricingError(f"{where}: expected an object")
    unknown = sorted(set(value) - set(PRICE_FIELDS))
    if unknown:
        raise PricingError(f"{where}: unknown field {unknown[0]!r} (expected "
                           f"{', '.join(PRICE_FIELDS)})")
    out = {}
    for field in PRICE_FIELDS:
        price = value.get(field)
        if isinstance(price, bool) or not isinstance(price, int) or not 0 <= price <= MAX_PRICE:
            raise PricingError(f"{where}.{field}: expected a non-negative integer (micro-USD "
                               f"per 1M tokens) of at most {MAX_PRICE}")
        out[field] = price
    return out


def _model_name(name) -> str:
    if not isinstance(name, str) or not name or len(name) > MAX_MODEL_NAME:
        raise PricingError(f"models: a model name must have 1 to {MAX_MODEL_NAME} characters")
    if name != name.strip() or any(unicodedata.category(c).startswith("C") for c in name):
        raise PricingError(f"models: model name {name!r} has surrounding spaces or "
                           "control characters")
    return name


class Pricing:
    def __init__(self, default: dict[str, int] | None = None,
                 models: dict[str, dict[str, int]] | None = None):
        self.default = dict(default or DEFAULT_PRICES)
        self.models = {name: dict(prices) for name, prices in (models or {}).items()}

    @classmethod
    def parse(cls, data, require_default: bool = True) -> "Pricing":
        """The checked table. A file may omit "default" (DEFAULT_PRICES); the admin API
        sends the whole table, so it must not."""
        if not isinstance(data, dict):
            raise PricingError("pricing: expected an object")
        unknown = sorted(set(data) - {"default", "models"})
        if unknown:
            raise PricingError(f"pricing: unknown field {unknown[0]!r} (expected default, "
                               "models)")
        if require_default and "default" not in data:
            raise PricingError("default: required")
        default = _prices(data.get("default", DEFAULT_PRICES), "default")
        models = data.get("models", {})
        if not isinstance(models, dict):
            raise PricingError("models: expected an object")
        if len(models) > MAX_MODELS:
            raise PricingError(f"models: at most {MAX_MODELS} models")
        return cls(default, {_model_name(name): _prices(prices, f"models.{name}")
                             for name, prices in models.items()})

    @classmethod
    def load(cls, path: Path) -> "Pricing":
        try:
            data = json.loads(Path(path).read_text())
        except ValueError as error:
            raise PricingError(f"{path}: {error}") from None
        try:
            return cls.parse(data, require_default=False)
        except PricingError as error:
            raise PricingError(f"{path}: {error}") from None

    def to_dict(self) -> dict:
        return {"default": dict(self.default),
                "models": {name: dict(prices) for name, prices in self.models.items()}}

    def dumps(self) -> str:
        """The prices file: one line per entry, like pricing.example.json."""
        line = lambda prices: json.dumps(prices, separators=(", ", ": "))  # noqa: E731
        models = ",\n".join(f"    {json.dumps(name)}: {line(prices)}"
                            for name, prices in self.models.items())
        return (f'{{\n  "default": {line(self.default)},\n  "models": '
                + (f"{{\n{models}\n  }}" if models else "{}") + "\n}\n")

    def for_model(self, model) -> dict[str, int]:
        return self.models.get(model, self.default) if isinstance(model, str) else self.default

    def cost(self, model, prompt_tokens: int, cached_tokens: int, generated_tokens: int) -> int:
        """Cost in micro-USD, rounded up. Gufo's prompt_tokens counts the whole prompt,
        cached_tokens included, so only the rest is billed at the input price."""
        prices = self.for_model(model)
        prompt = max(0, int(prompt_tokens))
        cached = min(max(0, int(cached_tokens)), prompt)
        generated = max(0, int(generated_tokens))
        scaled = ((prompt - cached) * prices["input"] + cached * prices["cached_input"]
                  + generated * prices["output"])
        return -(-scaled // 1_000_000)


class PriceList:
    """The prices file and the live table. Readers take `current` once per use: it is
    replaced, never changed in place. Edits to the file by hand count from the next
    start; a save from the admin page replaces the file with the table it sent."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.current = Pricing.load(self.path)
        self._lock = threading.Lock()

    def save(self, data) -> Pricing:
        """Check DATA (the whole table), write it to the file atomically, keeping the
        file's mode, and make it the live table. Raises PricingError when invalid."""
        pricing = Pricing.parse(data)
        text = pricing.dumps().encode()
        with self._lock:
            target = self.path.resolve()
            try:
                mode = target.stat().st_mode & 0o7777
            except FileNotFoundError:
                mode = 0o600
            fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as file:
                    os.fchmod(file.fileno(), mode)
                    file.write(text)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.current = pricing
        return pricing
