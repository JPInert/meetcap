"""Word-list cards: a list term gets one card, plain words and repeats get none."""
import unittest

from mt.core.cards import ListCards
from mt.vocab import Vocab


class ListCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards = ListCards(Vocab.load())

    def test_term_cards_once_with_note_and_aliases(self):
        got = self.cards.scan('We should check the DMARC record first.')
        self.assertEqual([c['term'] for c in got], ['DMARC'])
        self.assertTrue(got[0]['note'])
        self.assertEqual(self.cards.scan('DMARC again, and DMARC once more.'), [])

    def test_alias_counts_as_the_same_term(self):
        self.assertEqual([c['term'] for c in self.cards.scan('Our MEM tenant is new.')], ['Intune'])
        self.assertEqual(self.cards.scan('Intune policies apply.'), [])

    def test_plain_words_get_no_card(self):
        self.assertEqual(self.cards.scan('The meeting starts at noon and we have a ticket.'), [])


if __name__ == '__main__':
    unittest.main()
