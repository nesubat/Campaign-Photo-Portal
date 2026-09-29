"""
Pure logic for New Store Kits - no database, no Flask, no Drive. app.py uses
it to validate what someone typed into a pack and to build the on-screen pack
lines and "Packed so far" report; drive_sync.py uses the very same functions
for the kit's Pack Log Sheet, so the Sheet can never disagree with the screen.

Vocabulary (the user's own words): an "item number" is a SERIES - J + 6
digits, e.g. J456789 - optionally followed by an INDEX after a dash, e.g.
J456789-02. The series field also accepts any free-text item description
("Window decals") for things that have no job-style number at all. Every
pack mostly uses one to three series, and the report works out, per series,
which indexes haven't been packed yet.

Everything here takes plain values or row-like objects (anything indexable
by column name - sqlite3.Row or a dict), so it's trivially testable on its own.
"""
import re

# re.ASCII: without it Python's \d also matches other scripts' digits
# ("J٤٥٦٧٨٩"), which the browser's own copy of these patterns (kit.js) never
# does - the two sides must agree on what counts as a job-style series.
J_SERIES_RE = re.compile(r"^[Jj]\d{6}$", re.ASCII)
J_WITH_INDEX_RE = re.compile(r"^([Jj]\d{6})\s*[-–—]\s*(\d{1,6})$", re.ASCII)

# The on-screen report lists at most this many missing indexes per series,
# then "& N more" - a typo like index 950 instead of 50 would otherwise print
# nine hundred numbers across a phone screen. The Sheet always gets the full list.
REPORT_TEXT_CAP = 30

# Longest series / item description accepted (after collapsing spaces).
SERIES_MAX_LEN = 80

_DIGITS_RE = re.compile(r"^[0-9]+$")

# "J456789-A1": shaped exactly like a series + index, but with letters where
# the index goes. That's an index typo, not an item description, so it's
# refused like a letter typed into the index field - rather than quietly
# stored as free text that reads like an item number on the Sheet and label
# yet never counts in the report. (A real description that merely starts
# with a series, "J456789 - window kit", is longer than an index and passes.)
_J_WITH_CODE_RE = re.compile(r"^[Jj]\d{6}\s*[-–—]\s*[0-9A-Za-z]{1,6}$", re.ASCII)

# Longest index accepted, in digits.
INDEX_MAX_DIGITS = 6

# Leading characters stripped off an index before it's read: people type the
# dash they'd write on the box ("-02"), and phone keyboards auto-convert "-"
# into en/em dashes.
_INDEX_LEAD_CHARS = " \t-–—"


def _as_text(raw):
    """JSON can hand us an int (idx: 2) as easily as a string - both are
    fine. Anything else (a list, an object) is a broken client, not a typo."""
    if raw is None:
        return ""
    if isinstance(raw, bool):
        raise ValueError("Invalid item number.")
    if isinstance(raw, (str, int)):
        return str(raw)
    raise ValueError("Invalid item number.")


def normalize_kit_name(raw):
    """(display, norm). Display keeps the person's own capitalisation but
    trims and collapses runs of spaces ("Store  12 " -> "Store 12"); norm is
    its casefold, which is what identifies a kit within a job - so "store 12"
    typed on another phone joins the same kit instead of starting a second one."""
    display = " ".join(("" if raw is None else str(raw)).split())
    return display, display.casefold()


def normalize_index(raw):
    """'' (no index) | "02" | "123". An index is the number after the dash on
    a box label, so it can only be digits - letters or symbols in it are a
    typo (the phone's number pad makes that rare, but a paste or a desktop
    keyboard can still send one), and letting them through would put
    something on the Sheet and the printed label that no box is called.
    Numbers are stored by VALUE, zero-padded to at least 2 digits ("2", "02"
    and "002" are the same box label), so a duplicate can't sneak in under a
    different spelling and the report can do arithmetic on them. Raises
    ValueError with a message that's safe to show the user as-is."""
    s = _as_text(raw).strip().lstrip(_INDEX_LEAD_CHARS).strip()
    if not s:
        return ""
    if not _DIGITS_RE.match(s):
        raise ValueError("Index numbers can only contain digits.")
    if len(s) > INDEX_MAX_DIGITS:
        raise ValueError(f"Index is too long - use up to {INDEX_MAX_DIGITS} digits (e.g. 02).")
    return str(int(s)).zfill(2)


def parse_item(series_raw, idx_raw):
    """What someone typed into the two item fields -> (series, series_norm,
    idx), ready to store. Raises ValueError with a user-facing message.

    - "j456789" + "2"      -> ("J456789", "J456789", "02")
    - "J456789-02" + ""    -> ("J456789", "J456789", "02")   pasted/typed combined form
    - "J456789-02" + "03"  -> ValueError (which one did they mean?)
    - "J456789" + "A1"     -> ValueError (an index is digits only)
    - "J456789-A1" + ""    -> ValueError (same typo, typed combined)
    - "Window  decals" + "" -> ("Window decals", "window decals", "")

    series_norm is what duplicates are detected on (see the unique index on
    kit_pack_items): the upper-case series for job-style numbers, the
    casefolded description for free text."""
    series = " ".join(_as_text(series_raw).split())
    if not series:
        raise ValueError("Enter an item number (e.g. J456123) or an item description.")
    if len(series) > SERIES_MAX_LEN:
        raise ValueError(f"Item number is too long - keep it under {SERIES_MAX_LEN} characters.")

    idx_text = _as_text(idx_raw)
    combined = J_WITH_INDEX_RE.match(series)
    if combined:
        if idx_text.strip().lstrip(_INDEX_LEAD_CHARS).strip():
            raise ValueError("Item number already includes an index - clear one of them.")
        series = combined.group(1).upper()
        return series, series, normalize_index(combined.group(2))
    if _J_WITH_CODE_RE.match(series):
        raise ValueError("Index numbers can only contain digits.")

    idx = normalize_index(idx_text)
    if J_SERIES_RE.match(series):
        series = series.upper()
        return series, series, idx
    return series, series.casefold(), idx


def item_label(series, idx):
    """"J456789-02" | "J456789" | "Window decals" - one entry, as shown on a
    removable chip and in "Added ..." messages."""
    return f"{series}-{idx}" if idx else series


def join_and(parts):
    """["02"] -> "02"; ["02", "03"] -> "02 & 03"; ["02", "03", "04"] -> "02, 03 & 04"."""
    parts = list(parts)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " & " + parts[-1]


def _in_entry_order(items):
    """Oldest entry first. The db already returns items by id, but sorting
    here too means a caller that filtered or merged lists (drive_sync picking
    one pack's items) can't accidentally reorder the report or buttons."""
    items = list(items)
    try:
        return sorted(items, key=lambda it: it["id"])
    except (KeyError, IndexError, TypeError):
        return items


def _index_sort_key(idx):
    # Numbers by value (so "10" sorts after "09", not after "01"). Only digits
    # can be entered now, but a kit started before that rule may still hold a
    # letter code ("A1") - it sorts after all the numbers instead of crashing.
    if _DIGITS_RE.match(idx):
        return (0, int(idx), idx)
    return (1, 0, idx)


def pack_groups(items):
    """One pack's items grouped the way they're shown and printed - one group
    per series (or free-text description), in the order each was first
    entered, with its indexes sorted:

        [{"series": "J456781", "bareItemId": None,
          "indexes": [{"id": 52, "idx": "02"}, {"id": 55, "idx": "05"}, ...]},
         {"series": "Window decals", "bareItemId": 60, "indexes": []}]

    bareItemId is the id of the entry added "as-is" (no index) for that
    series, or None. Every entry keeps its item id so the page can put a ×
    on each index inside a line like "J456781-02, 05, 06, 08 & 09" and
    remove exactly that one. pack_lines is built from this, so the grouped
    editor, the plain card text, the Sheet and the printed label can never
    disagree about what a pack holds.

    items: rows/dicts with id, series, series_norm, idx. Within one pack the
    unique index already rules out repeats; if a caller merges several packs'
    items, a repeated entry is listed once (the first one)."""
    groups = {}
    order = []
    for it in _in_entry_order(items):
        key = it["series_norm"]
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"series": it["series"], "bareItemId": None, "bare": False, "indexes": []}
            order.append(key)
        idx = it["idx"] or ""
        if not idx:
            if not group["bare"]:
                group["bare"] = True
                group["bareItemId"] = it["id"]
        elif all(entry["idx"] != idx for entry in group["indexes"]):
            group["indexes"].append({"id": it["id"], "idx": idx})

    result = []
    for key in order:
        group = groups[key]
        result.append({
            "series": group["series"],
            "bareItemId": group["bareItemId"],
            "indexes": sorted(group["indexes"], key=lambda entry: _index_sort_key(entry["idx"])),
        })
    return result


def pack_lines(items):
    """One pack's items as the lines shown on its card, written to the Sheet
    and printed on its label - the user's own example: J456789 with 02, 03,
    04 then J456781 with 01, 03, 02 reads

        J456789-02, 03 & 04
        J456781-01, 02 & 03

    Series appear in the order they were first entered (the order the packer
    worked in); indexes within a series are sorted, since the order someone
    happened to pick boxes up in isn't meaningful. A series that was also
    added "as-is" (no index) gets its own bare line first. Built from
    pack_groups - see there."""
    lines = []
    for group in pack_groups(items):
        if group["bareItemId"] is not None:
            lines.append(group["series"])
        if group["indexes"]:
            lines.append(f"{group['series']}-{join_and(entry['idx'] for entry in group['indexes'])}")
    return lines


def series_buttons(items):
    """Distinct job-style series (J + 6 digits) in the order they were FIRST
    used anywhere in the kit - the quick-pick buttons under the series field,
    shared by every collaborator. First-use order (not most-recent) keeps a
    button from jumping around under someone's thumb when a colleague adds
    to a different series. Free-text descriptions never get a button."""
    seen = set()
    buttons = []
    for it in _in_entry_order(items):
        series = it["series"]
        if not J_SERIES_RE.match(series):
            continue
        series = series.upper()
        if series not in seen:
            seen.add(series)
            buttons.append(series)
    return buttons


def series_report(items, full=True):
    """The "Packed so far" summary, one entry per job-style series that has
    at least one numeric index, in first-use order:

        {"series": "J456789", "lastNumber": "09", "missing": ["02", "03", "07", "08"],
         "missingCount": 4, "complete": False,
         "text": "J456789 - Missing numbers: 02, 03, 07 & 08 until 09."}

    Nothing missing reads "J456789 - Packed everything until 09." - the same text
    on the kit page, in the Final Submit dialog and on the Drive Sheet.

    "Missing" means every number from 1 up to the highest one packed that no
    pack contains yet - it can't know about numbers beyond the last one
    anybody entered, so the text says what the last number is and lets a
    person judge whether that's really the end. Numbers are padded to the
    widest index seen for that series (at least 2 digits), so 950 alongside
    02 reads "001, 002, ...". Free text isn't countable and is left out (as
    is a letter-code index a kit may still hold from before indexes became
    digits-only - see normalize_index). `text` is capped at REPORT_TEXT_CAP
    numbers. With full=True (drive_sync - the Sheet keeps the whole list)
    `missing` is the FULL list; with full=False (the kit page, every poll)
    it holds only the first REPORT_TEXT_CAP, found by walking the gaps - one
    mistyped index like 999999 would otherwise build a million-entry list on
    every phone's 4-second poll just to print thirty of them. missingCount is
    always the true total.

    Callers choose which items count: app.py passes only SAVED packs' items
    (the report is "what has been packed"), drive_sync passes everything."""
    numeric = {}
    order = []
    for it in _in_entry_order(items):
        series = it["series"]
        if not J_SERIES_RE.match(series):
            continue
        series = series.upper()
        if series not in numeric:
            numeric[series] = set()
            order.append(series)
        idx = it["idx"] or ""
        if _DIGITS_RE.match(idx):
            numeric[series].add(idx)

    report = []
    for series in order:
        indexes = numeric[series]
        if not indexes:
            continue
        used = {int(i) for i in indexes}
        last = max(used)
        width = max(2, max(len(i) for i in indexes))
        # "00" (index 0) is outside 1..last, so it doesn't reduce the count.
        missing_count = last - len(used - {0})
        if full:
            missing = [str(n).zfill(width) for n in range(1, last + 1) if n not in used]
        else:
            missing = []
            n = 1
            while len(missing) < REPORT_TEXT_CAP and n <= last:
                if n not in used:
                    missing.append(str(n).zfill(width))
                n += 1
        last_text = str(last).zfill(width)
        if not missing_count:
            text = f"{series} - Packed everything until {last_text}."
        elif missing_count > REPORT_TEXT_CAP:
            shown = ", ".join(missing[:REPORT_TEXT_CAP])
            text = (
                f"{series} - Missing numbers: {shown} "
                f"& {missing_count - REPORT_TEXT_CAP} more until {last_text}."
            )
        else:
            text = f"{series} - Missing numbers: {join_and(missing)} until {last_text}."
        report.append({
            "series": series,
            "lastNumber": last_text,
            "missing": missing,
            "missingCount": missing_count,
            "complete": not missing_count,
            "text": text,
        })
    return report
