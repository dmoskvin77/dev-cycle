import unittest

from booking import BookingSlot, SlotTaken


class BookingSlotTest(unittest.TestCase):
    def setUp(self):
        self.slot = BookingSlot()

    # verifies: USRSTR-1/ACPT-1
    def test_submit_returns_request_number_and_pending_status(self):
        request_id, status = self.slot.submit()

        self.assertGreater(request_id, 0)
        self.assertEqual(status, "pending")

    # verifies: USRSTR-1/ACPT-2
    def test_reading_request_after_confirmation(self):
        request_id, _ = self.slot.submit()
        self.slot.confirm(request_id, is_admin=True)

        self.assertEqual(self.slot.status(request_id), "confirmed")

    # verifies: USRSTR-2/ACPT-1
    def test_only_administrator_can_confirm_a_request(self):
        request_id, _ = self.slot.submit()

        with self.assertRaises(PermissionError):
            self.slot.confirm(request_id, is_admin=False)
        self.assertEqual(self.slot.status(request_id), "pending")

        self.slot.confirm(request_id, is_admin=True)
        self.assertEqual(self.slot.status(request_id), "confirmed")

    # verifies: USRSTR-2/ACPT-2
    def test_second_request_cannot_be_confirmed_for_the_same_slot(self):
        first, _ = self.slot.submit()
        second, _ = self.slot.submit()
        self.slot.confirm(first, is_admin=True)

        with self.assertRaises(SlotTaken):
            self.slot.confirm(second, is_admin=True)
        self.assertEqual(self.slot.status(first), "confirmed")
        self.assertEqual(self.slot.status(second), "pending")
