# safety.py
import re

# Hard backstop against sexual/explicit content. Personalities are instructed via their
# system prompt to keep things non-sexual (romance/flirting is fine, explicit content
# isn't), but instructions alone aren't a guarantee — this regex catches it either way,
# on both the user's prompt and the model's reply. Intentionally blunt: it's better to
# over-block here than let something through.
_EXPLICIT_TERM_RE = re.compile(
    r'\b(' + '|'.join([
        r'sex(?:ual|y)?', r'porn\w*', r'nude\w*', r'naked', r'orgasm\w*',
        r'masturbat\w*', r'penis', r'vagina', r'dick', r'cock', r'pussy', r'cum\w*',
        r'horny', r'fetish', r'kink\w*', r'bdsm', r'dominatrix', r'submissive',
        r'blowjob', r'handjob', r'deepthroat', r'anal', r'fellatio', r'cunnilingus',
        r'ejaculat\w*', r'creampie', r'squirt\w*', r'orgy', r'threesome', r'incest',
        r'molest\w*', r'rape', r'nsfw', r'thot', r'slut\w*', r'whore',
        r'fuck(?:ing)?\s+(?:me|you|her|him|us|them)',
    ]) + r')\b',
    re.IGNORECASE,
)

SAFE_REFUSAL = "Let's keep it PG, bro — try a different topic."


def is_sexual(text: str) -> bool:
    return bool(text) and bool(_EXPLICIT_TERM_RE.search(text))
