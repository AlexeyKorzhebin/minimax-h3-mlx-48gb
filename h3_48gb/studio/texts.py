"""Text checks shared by the cast card and the lint rules."""
from __future__ import annotations

import re

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
