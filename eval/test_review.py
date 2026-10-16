"""A short substantive question ends the previous answer; a backchannel does not."""
import unittest

from mt.review import metrics


class AnswerBoundaries(unittest.TestCase):
    def test_short_question_does_not_duplicate_next_answer(self):
        tr = [
            {'who': 'them', 'start': 0, 'end': 4, 'text': 'Tell me about your support experience?'},
            {'who': 'me', 'start': 5, 'end': 10, 'text': 'I support Windows endpoints.'},
            {'who': 'them', 'start': 11, 'end': 13, 'text': 'Why leave?'},
            {'who': 'me', 'start': 14, 'end': 19, 'text': 'I want broader responsibility.'},
        ]
        qa = metrics(tr, 0)['qa']
        self.assertEqual([(q['answer'], q['answer_s'], q['pause_s']) for q in qa],
                         [('I support Windows endpoints.', 5, 1),
                          ('I want broader responsibility.', 5, 1)])

    def test_short_acknowledgement_keeps_answer_together(self):
        tr = [
            {'who': 'them', 'start': 0, 'end': 4, 'text': 'Tell me about your support experience?'},
            {'who': 'me', 'start': 5, 'end': 10, 'text': 'I support Windows endpoints.'},
            {'who': 'them', 'start': 11, 'end': 12, 'text': 'Okay.'},
            {'who': 'me', 'start': 13, 'end': 18, 'text': 'I also maintain the runbooks.'},
        ]
        qa = metrics(tr, 0)['qa']
        self.assertEqual([(q['answer'], q['answer_s']) for q in qa],
                         [('I support Windows endpoints. I also maintain the runbooks.', 10)])

    def test_trailing_stt_fragment_does_not_hide_answered_question(self):
        tr = [
            {'who': 'them', 'start': 0, 'end': 6,
             'text': 'Our team uses ServiceNow and an RMM called NinjaOne, have you used those? That'},
            {'who': 'me', 'start': 7, 'end': 12, 'text': 'I have used ServiceNow for three years.'},
        ]
        qa = metrics(tr, 0)['qa']
        self.assertEqual([q['answer'] for q in qa], ['I have used ServiceNow for three years.'])


if __name__ == '__main__':
    unittest.main()
