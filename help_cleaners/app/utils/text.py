import re

from app.config import MAX_NAME_LEN


_ALLOWED_RE = re.compile(r"^[A-Za-zА-Яа-я0-9_\-]{1,%d}$" % MAX_NAME_LEN)


def is_valid_display_name(name: str) -> bool:
	if not name:
		return False
	name = name.strip()
	if len(name) == 0 or len(name) > MAX_NAME_LEN:
		return False
	return bool(_ALLOWED_RE.match(name)) 