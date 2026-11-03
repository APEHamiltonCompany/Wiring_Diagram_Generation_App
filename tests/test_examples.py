"""
Regression tests: the three approved projects (Sanofi, AstraZeneca, Exact).

Each test drives the same Session the GUI uses, with the same manual choices
that were made for that project, and checks the approved circuit layout,
the CSV round trip and the generated .drawio.

Run:  python -m pytest tests -q     (or: python tests/test_examples.py)
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.session import Session                     # noqa: E402
from app.pn_overrides import PNOverrides            # noqa: E402
from app.csv_io import read_csv                     # noqa: E402
from app.drd import DRD                             # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
DRD_JSON = os.path.join(ROOT, "data", "drd.json")


def new_session():
    # empty, in-memory part # rules so a user's saved rules never affect tests
    return Session(DRD.load(DRD_JSON), "test", overrides=PNOverrides(path=None))


def circuits(res):
    return {c.num: (round(c.load, 1), sorted(l.unit.name for l in c.loads)) for c in res.circuits}


def map_pn(s, pn, names):
    e = next(x for x in s.state.excluded if x.line and x.line.part_number == pn)
    s.map_line(e.key, names)


class Base(unittest.TestCase):
    def roundtrip(self, s, tag):
        d = tempfile.mkdtemp()
        csv_p = os.path.join(d, tag + ".csv")
        dio_p = os.path.join(d, tag + ".drawio")
        s.export_csv(csv_p)
        m = read_csv(csv_p)
        self.assertEqual(len(m.devices), len(s.result.model.devices))
        self.assertEqual(len(m.connections), len(s.result.model.connections))
        s.generate_diagram(dio_p, csv_p)
        self.assertTrue(os.path.getsize(dio_p) > 1000)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "validate_diagram.py"),
                            dio_p], capture_output=True, text=True)
        self.assertIn("0 FAIL", r.stdout, r.stdout[-2000:])
        # StyleGuide 9.1: routed lines (waypoints, 10px lanes, no overlaps, no lines through blocks)
        self.assertIn("[PASS]  Routing (9.1)", r.stdout, r.stdout[-2000:])
        self.assertNotIn("zig-zag", r.stdout, r.stdout[-2000:])
        return r.stdout


class TestSanofi(Base):
    def setUp(self):
        s = new_session()
        s.load_order(os.path.join(FIX, "Sanofi_SO-292921.xlsx"))
        s.add_customer("BioTek Epoch 2")
        s.add_pipettor("VANTAGEline", "L14-20")
        s.apply_pairing()
        self.s = s
        self.res = s.run_analysis()

    def test_sorting_by_part_number_only(self):
        names = self.s.state.names()
        for n in ("L3 PC", "HHS Control Box", "Inheco ODTC Control Box", "Hamilton Heater Shaker"):
            self.assertIn(n, names)
        hhs = next(d for d in self.s.state.relevant if d.name == "Hamilton Heater Shaker")
        self.assertEqual(hhs.qty, 4)

    def test_circuits(self):
        self.assertTrue(self.res.ok, self.res.issues)
        c = circuits(self.res)
        self.assertEqual(len(c), 4)
        self.assertEqual(c[1], (12.0, ["L3 PC"]))
        self.assertEqual(c[2][1], ["BioTek Epoch 2", "Inheco ODTC Control Box"])
        self.assertEqual(c[3][0], 8.3)        # 4.3A HHS box + 4A MPE box
        self.assertEqual(c[4][1], ["VANTAGEline"])

    def test_children_have_no_circuit(self):
        loaded = {l.unit.name for c in self.res.circuits for l in c.loads}
        for n in ("Hamilton Heater Shaker", "Inheco ODTC", "Hamilton [MPE] Deck Module"):
            self.assertNotIn(n, loaded)

    def test_diagram(self):
        out = self.roundtrip(self.s, "sanofi")
        self.assertIn("4 circuit ellipse(s)", out)


class TestAstraZeneca(Base):
    def setUp(self):
        s = new_session()
        s.load_order(os.path.join(FIX, "AstraZeneca_QTE-056533.xlsx"))
        # DRD part numbers for these are still blank -> mapped by hand
        map_pn(s, "6610032-01", ["BioTek 405"])
        map_pn(s, "0207846-001", ["Revvity VICTOR Nivo"])
        map_pn(s, "0207358-003", ["Ziath Mirage"])
        nivo = next(d for d in s.state.relevant if d.name == "Revvity VICTOR Nivo")
        s.edit_device(nivo.key, amps=3.5)          # largest draw between swaps
        s.add_pipettor("VANTAGEline", "C19")
        s.apply_pairing()
        s.options.over_80_approved = True
        self.s = s
        self.res = s.run_analysis()

    def test_pairing_adds_pump(self):
        self.assertIn("BioTek Pump", self.s.state.names())

    def test_circuits(self):
        self.assertTrue(self.res.ok, self.res.issues)
        c = circuits(self.res)
        self.assertEqual(len(c), 3)
        strip = [x for x in self.res.circuits if x.power_strip]
        self.assertEqual(len(strip), 1)
        self.assertAlmostEqual(strip[0].load, 12.6, places=1)
        self.assertEqual(self.res.hhs.get("mode"), "periphery")

    def test_80_percent_blocks_without_approval(self):
        self.s.options.over_80_approved = False
        res = self.s.run_analysis()
        self.assertEqual(len(res.circuits), 4)

    def test_diagram(self):
        self.roundtrip(self.s, "az")


class TestExact(Base):
    def setUp(self):
        s = new_session()
        s.load_order(os.path.join(FIX, "Exact_SO-294012.xlsx"))
        s.add_customer("Agilent PlateLoc")
        s.add_customer("IntelliXcap Decapper")
        s.add_pipettor("STARline", "C13")
        s.apply_pairing()
        self.s = s
        self.res = s.run_analysis()

    def test_circuits(self):
        self.assertTrue(self.res.ok, self.res.issues)
        self.assertEqual(len(self.res.circuits), 3)

    def test_serial_hub_and_centrifuge_port(self):
        names = {u.name for u in self.res.units}
        self.assertTrue(any("Serial Hub" in n for n in names), names)
        conns = [(c.source, c.target, c.target_port) for c in self.res.model.connections]
        self.assertTrue(any("Centrifuge" in a and "Div2" in (p or "") for a, b, p in conns), conns)

    def test_hhc_on_control_box(self):
        self.assertEqual(self.res.hhs.get("mode"), "control_box")

    def test_diagram(self):
        out = self.roundtrip(self.s, "exact")
        self.assertIn("3 circuit ellipse(s)", out)


def layout(res):
    return {c.num: sorted(l.key for l in c.loads) for c in res.circuits}


class TestCircuitMoves(unittest.TestCase):
    """Circuits table edits: only the moved loads change circuit."""
    def setUp(self):
        s = new_session()
        s.load_order(os.path.join(FIX, "Exact_SO-294012.xlsx"))
        s.add_customer("Agilent PlateLoc")
        s.add_customer("IntelliXcap Decapper")
        s.add_pipettor("STARline", "C13")
        s.apply_pairing()
        self.s = s
        self.base = layout(s.run_analysis())

    def where(self, key):
        return next(n for n, keys in layout(self.s.result).items() if key in keys)

    def test_move_keeps_other_loads(self):
        src = self.where("IntelliXcap Decapper")
        dst = next(n for n in self.base if n not in (src, 1))
        res = self.s.move_loads(["IntelliXcap Decapper"], dst)
        after = layout(res)
        self.assertIn("IntelliXcap Decapper", after[dst])
        for n, keys in self.base.items():
            for k in keys:
                if k != "IntelliXcap Decapper":
                    self.assertEqual(self.where(k), n, k)
        self.assertEqual(self.s.moved, {"IntelliXcap Decapper"})

    def test_swap(self):
        a, b = "IntelliXcap Decapper", "HHS Control Box"
        na, nb = self.where(a), self.where(b)
        self.assertNotEqual(na, nb)
        self.s.swap_loads(a, b)
        self.assertEqual((self.where(a), self.where(b)), (nb, na))

    def test_new_circuit(self):
        res = self.s.move_loads(["IntelliXcap Decapper"], "new")
        self.assertEqual(len(res.circuits), len(self.base) + 1)
        self.assertEqual(layout(res)[len(res.circuits)], ["IntelliXcap Decapper"])

    def test_empty_circuit_is_removed_and_numbers_stay_sequential(self):
        src = self.where("IntelliXcap Decapper")
        keys = self.base[src]
        res = self.s.move_loads(keys, "new")
        self.assertEqual([c.num for c in res.circuits], list(range(1, len(res.circuits) + 1)))
        self.assertEqual(len(res.circuits), len(self.base))
        self.assertEqual(sorted(layout(res)[len(res.circuits)]), sorted(keys))
        self.assertEqual(self.s.options.manual_circuits["IntelliXcap Decapper"], len(res.circuits))

    def test_main_pc_stays_put(self):
        self.s.move_loads(["HHS Control Box"], 1)
        self.assertEqual(self.where("Standard PC"), 1)
        self.assertEqual(self.where("HHS Control Box"), 1)

    def test_clear_and_device_change_reset(self):
        self.s.move_loads(["IntelliXcap Decapper"], "new")
        self.assertEqual(layout(self.s.clear_pins()), self.base)
        self.s.move_loads(["IntelliXcap Decapper"], "new")
        self.s.add_customer("Ziath Mirage")
        res = self.s.run_analysis()
        self.assertFalse(self.s.options.manual_circuits)
        self.assertTrue(any("circuit moves were cleared" in w for w in res.warnings))


class TestDedicatedCircuitMoves(unittest.TestCase):
    def setUp(self):
        s = new_session()
        s.load_order(os.path.join(FIX, "AstraZeneca_QTE-056533.xlsx"))
        map_pn(s, "6610032-01", ["BioTek 405"])
        map_pn(s, "0207846-001", ["Revvity VICTOR Nivo"])
        map_pn(s, "0207358-003", ["Ziath Mirage"])
        s.add_pipettor("VANTAGEline", "C19")
        s.apply_pairing()
        s.options.over_80_approved = True
        s.options.isolate_vantage = True
        self.s = s
        self.res = s.run_analysis()

    def test_load_on_dedicated_circuit_blocks(self):
        ded = next(c.num for c in self.res.circuits if c.dedicated)
        res = self.s.move_loads(["Ziath Mirage"], ded)
        self.assertFalse(res.ok)
        self.assertTrue(any("dedicated" in i for i in res.issues), res.issues)

    def test_vantage_onto_shared_circuit_blocks(self):
        vid = next(u.id for u in self.res.units if u.role == "pipettor")
        res = self.s.move_loads([vid], 1)
        self.assertFalse(res.ok)
        self.assertTrue(any("dedicated" in i for i in res.issues), res.issues)


class TestValidator(unittest.TestCase):
    """The routing check must catch the problems seen in the v2.0.0 diagrams."""
    def _run(self, xml):
        d = tempfile.mkdtemp(); p = os.path.join(d, "x.drawio")
        with open(p, "w") as f:
            f.write(xml)
        return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "validate_diagram.py"), p],
                              capture_output=True, text=True).stdout

    def _doc(self, edges):
        v = ('<mxCell id="a" value="A" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="100" y="100" width="100" height="60" as="geometry"/></mxCell>'
             '<mxCell id="b" value="B" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="400" y="300" width="100" height="60" as="geometry"/></mxCell>'
             '<mxCell id="m" value="M" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="250" y="100" width="60" height="60" as="geometry"/></mxCell>')
        e = "".join(f'<mxCell id="e{i}" style="edgeStyle=orthogonalEdgeStyle;strokeColor=#000000;" edge="1" parent="1" source="{s}" target="{t}"><mxGeometry relative="1" as="geometry">{p}</mxGeometry></mxCell>'
                    for i, (s, t, p) in enumerate(edges))
        return f'<mxfile><diagram><mxGraphModel gridSize="10"><root><mxCell id="0"/><mxCell id="1" parent="0"/>{v}{e}</root></mxGraphModel></diagram></mxfile>'

    def test_bent_line_without_waypoints_fails(self):
        self.assertIn("no waypoints", self._run(self._doc([("a", "b", "")])))

    def test_line_through_block_fails(self):
        pts = '<Array as="points"><mxPoint x="450" y="130"/></Array>'
        self.assertIn("passes through block", self._run(self._doc([("a", "b", pts)])))

    def test_overlap_and_lane_fail(self):
        p1 = '<Array as="points"><mxPoint x="150" y="330"/></Array>'
        p2 = '<Array as="points"><mxPoint x="155" y="335"/></Array>'
        out = self._run(self._doc([("a", "b", p1), ("a", "b", p1), ("a", "b", p2)]))
        self.assertIn("on top of each other", out)
        self.assertIn("closer than 10px", out)

    def test_clean_route_passes(self):
        pts = '<Array as="points"><mxPoint x="150" y="330"/></Array>'
        self.assertIn("[PASS]  Routing (9.1)", self._run(self._doc([("a", "b", pts)])))


class TestBayerLayout(unittest.TestCase):
    """Bayer SO-283791: 31 devices, 3 hubs, 9 circuits (hand-made diagram ~1215x1210)."""
    @classmethod
    def setUpClass(cls):
        import time
        from app.drawio_gen import DrawioGenerator
        cls.g = DrawioGenerator(read_csv(os.path.join(ROOT, "samples", "Bayer_SO-283791_V2_power_distribution.csv")))
        t = time.time()
        cls.xml = cls.g.generate()
        cls.seconds = time.time() - t

    def test_all_lines_routed(self):
        self.assertEqual([(c.source, c.target) for c, _a, _b in self.g.route_fail], [])

    def test_fast(self):
        self.assertLess(self.seconds, 30)

    def test_page_size(self):
        rects = [r for cid, r in self.g.rect.items() if self.g.col_of.get(cid) is not None]
        self.assertLessEqual(max(r.x2 for r in rects), 1.3 * 1215)
        self.assertLessEqual(max(r.y2 for r in rects), 1.3 * 1210)

    def test_off_deck_split_one_header_no_inner_separator(self):
        self.assertEqual(self.g.split, {3})
        self.assertEqual(self.xml.count("Off Deck Devices"), 1)
        self.assertEqual(len([s for s in self.g.separators if s[0] == "v"]), 7)
        xs = {int(r.x) for cid, r in self.g.rect.items() if self.g.col_of.get(cid) == 3}
        self.assertEqual(len(xs), 2)

    def test_pcs_in_a_row_laptop_level_with_xcytomatic(self):
        r = lambda ref: self.g.rect[self.g.cell[ref]]
        self.assertEqual(r("Standard PC #1").y, r("Standard PC #2").y)
        self.assertEqual(r("XM30 Laptop").y, r("XcytoMatic 30").y)
        hubs = sorted(self.g.hubs, key=lambda hb: r(hb.id).y)
        self.assertEqual(hubs[-1].id, "Serial Hub")


class TestRules(unittest.TestCase):
    def test_customer_device_must_be_drd(self):
        s = new_session()
        with self.assertRaises(ValueError):
            s.add_customer("Totally Made Up Device")

    def test_description_never_matches(self):
        from app.review import sort_order, OrderLine
        st = sort_order([OrderLine("NOPE-000", "Hamilton Heater Shaker HHS", 1)], DRD.load(DRD_JSON))
        self.assertEqual(st.relevant, [])
        self.assertEqual(len(st.excluded), 1)

    def test_export_blocked_on_issue(self):
        s = new_session()
        s.add_custom({"name": "Mystery box", "role": "powered", "amperage": None,
                      "amperage_status": "varies", "power_cable": "C13 to NEMA 5-15"})
        s.add_pipettor("STARline", "C13")
        res = s.run_analysis()
        self.assertFalse(res.ok)
        with self.assertRaises(RuntimeError):
            s.export_csv(os.path.join(tempfile.mkdtemp(), "x.csv"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
