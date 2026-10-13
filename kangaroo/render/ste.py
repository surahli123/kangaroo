"""The report's writing rules: the ASD-STE100 (Simplified Technical English) rules that a
script can test, applied to every sentence the agent writes.

Tested:
- a sentence has at most 20 words if it is an instruction, 25 words if it is a description;
- a paragraph or a list item has at most 6 sentences;
- no contractions ("isn't", "can't");
- no -ing verb forms (technical names are not tested);
- none of the house list of words that the templates avoid (informal words, phrasal verbs and
  words with more than one meaning, for example "fell", "rose", "anywhere", "push").
Not tested: the approved dictionary (it is licensed), noun clusters and passive voice.

Team text is not tested: metric, cut, arm and experiment names, text in backticks or quotes,
statement references, and what a person wrote on a card.
"""

import re

INSTRUCTION_MAX, DESCRIPTION_MAX, SENTENCES_MAX = 20, 25, 6
IMPERATIVES = {"get", "run", "ask", "prepare", "find", "fix", "wait", "examine", "make", "use", "do", "give", "tell",
               "record", "name", "stop", "keep", "escalate", "ship"}
NOT_ING = {"nothing", "something", "anything", "everything", "thing", "things", "during", "string", "strings", "bring",
           "ring", "king", "spring", "wing", "sing", "sting", "swing",
           # technical nouns (STE allows -ing as part of a technical name)
           "logging", "ranking", "rankings", "routing", "blending", "pinning", "sampling", "training", "billing"}
HOUSE = {"fell", "rose", "anywhere", "push", "pushes", "pushed", "figure", "basically", "really", "very", "lots",
         "stuff", "okay", "ok", "gonna", "via", "e.g", "i.e", "etc", "hence", "thus", "whilst", "amongst"}
CONTRACTION = re.compile(r"\b\w+(?:n't|'re|'ll|'ve|'d|'m)\b", re.I)


def _sentences(text):
    return [s for s in re.split(r"(?<=[.?!])\s+(?=[A-Z0-9(\"'])", text.strip()) if s.strip()]


def _clean(text, names):
    text = re.sub(r"`[^`]*`|\"[^\"]*\"|“[^”]*”|\[S\d+\]", " X ", text)
    for name in sorted(names, key=len, reverse=True):
        if name:
            text = re.sub(re.escape(name), " X ", text, flags=re.I)
    return re.sub(r"\*\*|__|^#+\s*|^\s*(?:[-*]|\d+\.)\s+", " ", text)


def units(markdown):
    """The text units of a Markdown document: paragraphs, list items and table cells."""
    out, para = [], []
    for line in markdown.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r"-+", c) for c in cells):
                out += [c for c in cells if c]
            continue
        if re.match(r"^\s*(?:[-*]|\d+\.)\s+", line) or line.startswith("#") or not line.strip():
            if para:
                out.append(" ".join(para))
                para = []
            if line.strip():
                out.append(line.strip())
            continue
        para.append(line.strip())
    if para:
        out.append(" ".join(para))
    return out


def check(markdown, names=(), skip_prefixes=("Provenance:", "Sources:")):
    """A list of (rule, text) for each break of the tested rules."""
    found = []
    for unit in units(markdown):
        if unit.startswith(skip_prefixes) or unit.startswith("#"):
            continue
        text = _clean(unit, names)
        sentences = _sentences(text)
        if len(sentences) > SENTENCES_MAX:
            found.append(("paragraph has more than 6 sentences", unit[:80]))
        for s in sentences:
            words = re.findall(r"[A-Za-z0-9][\w%.+\-/']*", s)
            limit = INSTRUCTION_MAX if words and words[0].lower() in IMPERATIVES else DESCRIPTION_MAX
            if len(words) > limit:
                found.append((f"sentence has {len(words)} words (limit {limit})", s.strip()[:120]))
        for m in CONTRACTION.finditer(text):
            found.append(("contraction", m.group(0)))
        for w in re.findall(r"\b[a-z]+ing\b", text):
            if w not in NOT_ING:
                found.append(("-ing form", w))
        for w in re.findall(r"\b[a-z][a-z.]*\b", text.lower()):
            if w.rstrip(".") in HOUSE:
                found.append(("word not in the house list", w))
    return found
