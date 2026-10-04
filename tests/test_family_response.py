import unittest

from granny.family_response import family_response


class FamilyResponseTests(unittest.TestCase):
    def test_natural_commitments(self):
        for text in ("Yes, I'm coming!", "I'm on my way", "I’ll be there", "yes",
                     "Sure, I can come", "We are coming now", "I will check on her",
                     "No problem, I'm coming", "Okay, I am heading over right now"):
            with self.subTest(text=text):
                self.assertEqual(family_response(text), "coming")

    def test_refusals_override_yes(self):
        for text in ("No", "I can't come", "Yes but I cannot come", "I'm not coming",
                     "I won't be there", "Sorry I am unavailable", "I cant come"):
            with self.subTest(text=text):
                self.assertEqual(family_response(text), "unavailable")

    def test_voicemail_questions_uncertainty_and_other_people_never_confirm(self):
        for text in ("", "hello", "maybe", "Yes I might come", "I'm not sure",
                     "I'll try to come", "I'm coming if I can", "He said I'm coming",
                     "Leave a message after the beep", "Yes hello leave your message",
                     "Are you coming?", "I was coming", "She is coming", "I heard you"):
            with self.subTest(text=text):
                self.assertEqual(family_response(text), "pending")

    def test_confidence_can_be_absent_but_not_invalid_or_low(self):
        for confidence in (None, "", "0.9", 1):
            self.assertEqual(family_response("I'm coming", confidence), "coming")
        for confidence in ("nan", "inf", "bad", "0.1", "1.1", "-1"):
            self.assertEqual(family_response("I'm coming", confidence), "pending")
