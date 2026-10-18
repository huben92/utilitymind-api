import unittest

from operations_app import flow_has_cycle


class MachineFlowTests(unittest.TestCase):
    def test_branches_and_merges_are_allowed(self):
        self.assertFalse(flow_has_cycle({(1, 2), (1, 3), (2, 4), (3, 4)}))

    def test_circular_connections_are_rejected(self):
        self.assertTrue(flow_has_cycle({(1, 2), (2, 3), (3, 1)}))


if __name__ == "__main__":
    unittest.main()
