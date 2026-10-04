"""Interpret a family contact's reply to the explicit 'can you come?' question."""
import math
import re

from .core import normalize_speech


def family_response(text, confidence=None):
    if confidence not in (None, ""):
        try:
            score = float(confidence)
        except (TypeError, ValueError):
            return "pending"
        if not math.isfinite(score) or score < .65 or score > 1:
            return "pending"
    text = text.lower().replace("’", "'")
    if "?" in text:
        return "pending"
    for contracted, expanded in (("i'll", "i will"), ("we'll", "we will"),
                                 ("we're", "we are"), ("won't", "will not"),
                                 ("don't", "do not"), ("can't", "cannot"), ("cant", "cannot")):
        text = re.sub(r"\b" + contracted + r"\b", expanded, text)
    text = normalize_speech(text)
    # Do not treat uncertainty, conditions, quotations or voicemail as a promise.
    if re.search(r"\b(maybe|might|try|trying|if|think|guess|perhaps|hopefully|voicemail|message|beep|said|say)\b", text) or "not sure" in text:
        return "pending"
    text = re.sub(r"^(no problem|no worries)\s*", "", text)
    if re.search(r"\b(no|not|cannot|unavailable|unable)\b", text):
        return "unavailable"
    yes = r"(?:yes|yeah|yep|sure|okay|ok|absolutely|of course)"
    promise = (r"(?:(?:i am|we are) (?:coming|on (?:my|our|the) way|heading over)|"
               r"(?:i|we) (?:can|will) (?:come|be there|go|check on (?:them|her|him))|"
               r"on (?:my|our|the) way)")
    timing = r"(?: (?:now|right now|immediately|as soon as (?:i|we) can))?"
    if re.fullmatch(rf"(?:{yes} )?{promise}{timing}(?: thank you)?|{yes}(?: i can)?", text):
        return "coming"
    return "pending"
