from __future__ import annotations

import unittest

import h5py

from app.storage.thatec_reader import ThatecRunReader
from tests.helpers import ROOT


REFERENCE_FILE = next(ROOT.glob("*.h5"))


class ThatecRunReaderReferenceTests(unittest.TestCase):
    def test_reference_file_describes_public_rows_devices_and_lazy_spectrum_slice(self) -> None:
        run = ThatecRunReader.describe(REFERENCE_FILE)

        self.assertGreaterEqual(len(run.rows), 1)
        self.assertGreaterEqual(len(run.devices), 1)
        measured = next(row for row in run.rows.values() if row.shape)
        values = ThatecRunReader.row_slice(REFERENCE_FILE, measured.id, 0)
        self.assertEqual(values.row_id, measured.id)
        self.assertEqual(values.checkpoint, 0)

    def test_reference_file_exposes_public_tree_and_row_metadata(self) -> None:
        tree = ThatecRunReader.tree(REFERENCE_FILE)
        def collect(nodes):
            found = set()
            for node in nodes:
                found.add(node.id)
                found.update(collect(node.children))
            return found

        self.assertEqual(collect(tree), set(ThatecRunReader.describe(REFERENCE_FILE).rows))
        first_id = next(iter(collect(tree)))
        self.assertEqual(ThatecRunReader.row(REFERENCE_FILE, first_id).id, first_id)

    def test_reference_file_preserves_every_tree_row_and_device_parameter(self) -> None:
        """Audit the importer against raw public THATEC data, not a fixture copy."""
        run = ThatecRunReader.describe(REFERENCE_FILE)
        tree = ThatecRunReader.tree(REFERENCE_FILE)

        def preorder(nodes):
            result = []
            for node in nodes:
                result.append((node.id, node.kind, node.label))
                result.extend(preorder(node.children))
            return result

        with h5py.File(REFERENCE_FILE, "r") as file:
            raw_tree = []
            for raw_id, kind, label in file["scan_definition/tree_view"].asstr()[()]:
                row_id = f"row_{int(str(raw_id).replace('row', '').strip()):02d}"
                raw_tree.append((row_id, str(kind), str(label)))
            raw_devices = {
                str(name): tuple((str(key), str(value)) for key, value in dataset.asstr()[()])
                for name, dataset in file["devices"].items()
            }

        self.assertEqual(preorder(tree), raw_tree)
        self.assertEqual(
            {device.name: device.values for device in run.devices}, raw_devices
        )


if __name__ == "__main__":
    unittest.main()
