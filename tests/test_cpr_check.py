"""
Tests for the optional "SO vs CPR" check (app/cpr_check.py).

Run:  python -m pytest tests -q     (or: python tests/test_cpr_check.py)
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.drd import DRD                                     # noqa: E402
from app.pn_overrides import PNOverrides                    # noqa: E402
from app.session import Session                             # noqa: E402
from app import review as rv                                # noqa: E402
from app import cpr_check as cc                             # noqa: E402
from app.so_cpr_check.models import (CprData, CprDevice, CprOption, CprSystem,
                                     SoLine)                # noqa: E402

DRD_JSON = os.path.join(ROOT, "data", "drd.json")
EX = os.environ.get("SO_CPR_EXAMPLES") or r"C:\repos\SO_CPR_Check\Example Files"


def new_session():
    return Session(DRD.load(DRD_JSON), "test", overrides=PNOverrides(path=None))


def _require(paths):
    missing = [p for p in paths if not os.path.exists(p)]
    return unittest.skipUnless(not missing, "missing example files: " + ", ".join(missing))


class TestNormalize(unittest.TestCase):
    def test_otp_normalization(self):
        self.assertEqual(cc.normalize_otp("OTP-BITK-0115  (https://www.fishersci...)"),
                         ["OTP-BITK-0115"])
        self.assertEqual(cc.normalize_otp("OTP-BRKS-0001 & OTP-AGLT-0025"),
                         ["OTP-BRKS-0001", "OTP-AGLT-0025"])
        self.assertEqual(cc.normalize_otp("No OTP"), [])
        self.assertEqual(cc.normalize_otp("Customer-provided (CAD models sent)"), [])


class TestPowered(unittest.TestCase):
    def test_powered_options(self):
        system = CprSystem("VANTAGE", "", False, "", "", [
            CprOption("Platform", "2.0m Pipettor", True),
            CprOption("Platform", "Logistics Cabinet", True),
            CprOption("Platform", "CAP (HEPA)", True),
            CprOption("Deck/Logistics Cabinet Slot Configuration", "AC Box: 2 Phase", True),
            CprOption("Deck", "AC Box: None", True),
            CprOption("Enclosures & Tables", "UV", False),
            CprOption("Platform", "Track Gripper", True),
        ])
        kinds = [o.kind for o in cc.powered_options(system)]
        self.assertEqual(kinds, ["Instrument", "Logistics cabinet", "HEPA/UV/CAP", "AC box"])


class TestQtyConflict(unittest.TestCase):
    def test_conflict_and_resolution(self):
        s = new_session()
        s.so_lines = [SoLine(1, "OTP-INHC-0173", "ODTC", 1.0, "", 0.0)]
        s.state = rv.sort_order([rv.OrderLine("OTP-INHC-0173", "ODTC", 1)], s.drd, s.overrides)
        chk = cc.check_data(
            CprData([], [CprDevice("OTP-INHC-0173", "INHECO", "ODTC 96 XL", "x"),
                         CprDevice("OTP-INHC-0173  (https://example.com)", "INHECO",
                                   "ODTC 96 XL", "x")], {}, None),
            s.so_lines)
        s.apply_cpr_check(chk)
        self.assertEqual(len(chk.conflicts), 1)
        c = chk.conflicts[0]
        self.assertEqual(c.so_qty, 1)
        self.assertEqual(c.cpr_qty, 2)
        self.assertEqual(chk.missing, [])
        s.add_pipettor("STARline")
        res = s.run_analysis()
        self.assertTrue(any("qty conflict" in i.lower() for i in res.issues))
        s.resolve_qty(c.key, 2)
        for d in s.state.relevant:
            if d.lines == ["OTP-INHC-0173"]:
                rec = s.drd.get(d.name) or {}
                self.assertEqual(d.qty, 2 * rec.get("kit_qty", 1))
        res = s.run_analysis()
        self.assertFalse(any("qty conflict" in i.lower() for i in res.issues))


class TestMissingDevice(unittest.TestCase):
    def test_missing_devices_added_and_cleared(self):
        s = new_session()
        chk = cc.check_data(
            CprData([], [CprDevice("OTP-INHC-0173", "INHECO", "ODTC 96 XL", "No"),
                         CprDevice("No OTP", "Revvity", "Victor Nivo", "No")], {}, None),
            s.so_lines)
        s.apply_cpr_check(chk)
        cpr_devs = [d for d in s.state.relevant
                    if d.source == rv.SRC_CPR and d.lines == ["OTP-INHC-0173"]]
        self.assertTrue(cpr_devs)
        vn = [e for e in s.state.excluded
              if e.line and "Victor Nivo" in e.line.description
              and e.reason == rv.REASON_CPR_NO_DRD]
        self.assertTrue(vn)
        s.clear_cpr_check()
        self.assertEqual(s.state.relevant, [])
        self.assertEqual(s.state.excluded, [])
        self.assertIsNone(s.cpr)


NATERA_SO = os.path.join(EX, "NonAZProjectExampleFiles", "Natera",
                         "SalesOrderConfirmation_SO-295702-1.xlsx")
NATERA_CPR = os.path.join(EX, "NonAZProjectExampleFiles", "Natera",
                          "CPR_NATERA_VANTAGE_ODTC.docx")


@_require([NATERA_SO, NATERA_CPR])
class TestNatera(unittest.TestCase):
    def test_natera(self):
        s = new_session()
        s.load_order(NATERA_SO)
        self.assertGreater(len(s.state.relevant) + len(s.state.excluded), 0)
        self.assertTrue(any("OTP-INHC-0173" in d.lines for d in s.state.relevant))
        s.run_cpr_check(NATERA_CPR)
        chk = s.cpr
        self.assertEqual(chk.missing, [])
        self.assertEqual(len(chk.conflicts), 1)
        self.assertEqual(chk.conflicts[0].part_number, "OTP-INHC-0173")
        kinds = {o.kind for o in chk.powered}
        self.assertIn("Logistics cabinet", kinds)
        self.assertIn("HEPA/UV/CAP", kinds)
        self.assertIn("Instrument", kinds)
        self.assertTrue(any(p.model == "VANTAGEline" for p in s.state.pipettors))
        self.assertTrue(any(e.reason == rv.REASON_CPR_OPTION for e in s.state.excluded))


CR_SO = os.path.join(EX, "NonAZProjectExampleFiles", "Charles River",
                     "SalesOrderConfirmation_SO-261021-10.xlsx")
CR_CPR = os.path.join(EX, "NonAZProjectExampleFiles", "Charles River",
                      "CPR_Charles River_APE_45543_ELISA STAR_SYS B v0.5.docx")


@_require([CR_SO, CR_CPR])
class TestCharlesRiver(unittest.TestCase):
    def test_charles_river(self):
        s = new_session()
        s.load_order(CR_SO)
        s.run_cpr_check(CR_CPR)
        chk = s.cpr
        self.assertEqual({d.otp for d in chk.missing}, {"OTP-BITK-0115", "OTP-MLDV-0007"})
        bitk = next(d for d in chk.missing if d.otp == "OTP-BITK-0115")
        self.assertEqual(bitk.cpr_qty, 2)
        dev_rows = {r.cpr_sk_pn: r.status for r in chk.rows
                    if r.type == "Device" and r.cpr_sk_pn}
        self.assertEqual(dev_rows.get("OTP-97091-22"), "Match")
        pns = {e.line.part_number for e in s.state.excluded if e.line}
        pns |= {pn for d in s.state.relevant for pn in d.lines}
        self.assertIn("6608628-01", pns)


HAM_SO = os.path.join(EX, "HamAustalia_ItemsMissingInSo",
                      "SalesOrderConfirmation_SO-294084-2.xlsx")
HAM_CPR = os.path.join(EX, "HamAustalia_ItemsMissingInSo",
                       "CPR_APE 113228_ University of South Wales_Upgrade_v0.2.docx")


@_require([HAM_SO, HAM_CPR])
class TestHamAustralia(unittest.TestCase):
    def test_ham_australia(self):
        s = new_session()
        s.load_order(HAM_SO)
        s.run_cpr_check(HAM_CPR)
        chk = s.cpr
        self.assertEqual(chk.missing, [])
        dev_rows = {r.cpr_sk_pn: r.status for r in chk.rows
                    if r.type == "Device" and r.cpr_sk_pn}
        for otp in ("OTP-QINS-0067", "OTP-DTLG-0001", "OTP-97091-20"):
            self.assertEqual(dev_rows.get(otp), "Match")
        self.assertTrue(chk.upgrade)


EXACT_DIR = os.environ.get("EXACT_296266_DIR") or r"C:\Users\Anthony.Zunino\OneDrive - Hamilton Company\Desktop\Projects\Project Specific Documents\Exact SO-296266"
EXACT_SO = os.path.join(EXACT_DIR, "DynamicsExport_639298545560985631.xlsx")
EXACT_CPR = os.path.join(EXACT_DIR, "CPR_Exact_Azenta_HHS_Tube_Rack___Centrifuge_v1.9.docx")


@_require([EXACT_SO, EXACT_CPR])
class TestExact296266(unittest.TestCase):
    def test_dynamics_export_detection(self):
        from app.so_cpr_check import so_reader
        self.assertTrue(so_reader.is_dynamics_export(EXACT_SO))
        self.assertFalse(so_reader.is_confirmation(EXACT_SO))
        self.assertEqual(Session.find_order_for_cpr(EXACT_CPR), EXACT_SO)

    def test_dynamics_export_lines(self):
        s = new_session()
        s.load_order(EXACT_SO)
        misc = [l for l in s.so_lines if l.product_number == "APE MISC"]
        self.assertTrue(any(abs(l.unit_price - 1100.0) < 0.01 and l.quantity == 8 for l in misc))

    def test_cpr_parts_and_system(self):
        from app.so_cpr_check.cpr_reader import read_cpr
        cpr = read_cpr(EXACT_CPR)
        self.assertEqual([p.sk_pn for p in cpr.parts],
                         ["0207857-001", "0207857-002", "0207857-003", "0207857-004"])
        self.assertEqual([p.price for p in cpr.parts], [1100.0, 3250.0, 3250.0, 14240.0])
        self.assertEqual(sorted(cpr.spec_names),
                         ["APE-111821-001", "APE-111821-002", "APE-111821-003", "APE-111821-004"])
        self.assertEqual(cpr.system.family, "STAR")
        self.assertTrue(cpr.system.upgrade)
        self.assertEqual(cpr.system.serial_number, "G343")

    def test_check_rows(self):
        s = new_session()
        s.load_order(EXACT_SO)
        s.run_cpr_check(EXACT_CPR)
        chk = s.cpr
        custom = [r for r in chk.rows if r.type == "Custom Part"]
        self.assertEqual(len(custom), 4)
        self.assertEqual({r.status for r in custom}, {"Match"})
        dev_rows = {r.cpr_sk_pn: r.status for r in chk.rows
                    if r.type == "Device" and r.cpr_sk_pn}
        self.assertEqual(dev_rows.get("OTP-AGLT-0025"), "Only in CPR")
        self.assertEqual(dev_rows.get("199700"), "Match")
        self.assertTrue(chk.upgrade)


FIXTURE_DYNAMICS = os.path.join(ROOT, "tests", "fixtures", "Exact_SO-294012.xlsx")


@_require([FIXTURE_DYNAMICS])
class TestDynamicsFixture(unittest.TestCase):
    def test_fixture_export(self):
        from app.so_cpr_check import so_reader
        self.assertTrue(so_reader.is_dynamics_export(FIXTURE_DYNAMICS))
        lines = so_reader.read_dynamics_export(FIXTURE_DYNAMICS)
        self.assertEqual(lines[0].product_number, "STAR-2")
        self.assertIn("ISWAP", lines[0].config_description)


if __name__ == "__main__":
    unittest.main(verbosity=2)
