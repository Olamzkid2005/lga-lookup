"""
Offline unit tests for lookup.py logic — no API calls, no file I/O.
Run with:  python test_lookup.py
"""
import sys, types, unittest

# ── Stub out argparse so lookup.py doesn't exit when imported ─────────
import argparse
_real_parse = argparse.ArgumentParser.parse_args
def _stub_parse(self, args=None, namespace=None):
    ns = argparse.Namespace(input="LGA Confirm.xlsx", output=None, sheet=0)
    return ns
argparse.ArgumentParser.parse_args = _stub_parse

# Stub requests so no network calls happen during import
import unittest.mock as mock
sys.modules["requests"] = mock.MagicMock()

import importlib.util, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Read only the function-definition portion of lookup.py (stop before data loading)
with open("lookup.py", encoding="utf-8") as f:
    source = f.read()

# Truncate at the data-loading section so we don't need a real file
cutoff = source.find("# ── Load data")
if cutoff == -1:
    cutoff = source.find("log.info(f\"Loading:")
func_source = source[:cutoff] if cutoff != -1 else source

# Execute the trimmed source in a fresh module namespace
mod = types.ModuleType("lookup")
mod.__file__ = "lookup.py"
with mock.patch("sys.exit"), \
     mock.patch("pandas.read_excel", return_value=mock.MagicMock()), \
     mock.patch("pandas.read_csv",   return_value=mock.MagicMock()):
    exec(compile(func_source, "lookup.py", "exec"), mod.__dict__)

normalise_state           = mod.normalise_state
infer_state_from_address  = mod.infer_state_from_address
clean_address             = mod.clean_address
split_concatenated        = mod.split_concatenated
build_query_variants      = mod.build_query_variants
keyword_lga_lookup        = mod.keyword_lga_lookup
extract_meaningful_tokens = mod.extract_meaningful_tokens
expand_abbreviations      = mod.expand_abbreviations


class TestNormaliseState(unittest.TestCase):
    def test_uppercase_abbrev(self):
        self.assertEqual(normalise_state("FCT"), "Federal Capital Territory")
    def test_lowercase(self):
        self.assertEqual(normalise_state("lagos"), "Lagos")
    def test_with_state_suffix(self):
        self.assertEqual(normalise_state("Lagos State"), "Lagos")
        self.assertEqual(normalise_state("IMO STATE"), "Imo")
    def test_typo_nassarawa(self):
        self.assertEqual(normalise_state("NASSARAWA"), "Nasarawa")
    def test_akwa_ibom(self):
        self.assertEqual(normalise_state("AKWA IBOM"), "Akwa Ibom")
    def test_blank(self):
        self.assertEqual(normalise_state(""), "")
        self.assertEqual(normalise_state("nan"), "")
    def test_yola_as_state(self):
        self.assertEqual(normalise_state("YOLA"), "Adamawa")


class TestInferState(unittest.TestCase):
    def test_oyo_in_address(self):
        self.assertEqual(
            infer_state_from_address("Heritage Mall, Cocoa Road Dugbe Ibadan Oyo St"),
            "Oyo"
        )
    def test_kano_in_address(self):
        self.assertEqual(
            infer_state_from_address("DANGWAURO ZARIA ROAD KANO"),
            "Kano"
        )
    def test_katsina_in_address(self):
        self.assertEqual(
            infer_state_from_address("MINISTRY OF FINANCE KATSINA STATE SECRET"),
            "Katsina"
        )
    def test_false_positive_new_lagos_road(self):
        # "New Lagos Road, Benin City" should NOT infer Lagos
        result = infer_state_from_address("80, NEW LAGOS ROAD, BENIN CITY, EDO STATE.")
        self.assertNotEqual(result, "Lagos")
    def test_imo_state_in_address(self):
        self.assertEqual(
            infer_state_from_address("76 OKIGWE ROAD, OWERRI, IMO STATE"),
            "Imo"
        )
    def test_rivers_in_address(self):
        result = infer_state_from_address("PLOT 152 TOMBIA STREET GRA PHASE 1 RIVERS STATE")
        self.assertEqual(result, "Rivers")
    def test_no_state(self):
        self.assertEqual(infer_state_from_address("12 Broad Street"), "")


class TestCleanAddress(unittest.TestCase):
    def test_strips_noise(self):
        result = clean_address("Plot 5, Block B, Flat 3, Lekki Phase 1")
        self.assertNotIn("plot", result.lower())
        self.assertNotIn("block", result.lower())
        self.assertNotIn("flat", result.lower())
    def test_strips_trailing_state(self):
        result = clean_address("76 OKIGWE ROAD, OWERRI, IMO STATE")
        self.assertNotIn("imo state", result.lower())
    def test_strips_nigeria(self):
        result = clean_address("14 Yeye Olofin Street Lekki Phase 1 Lagos Nigeria")
        self.assertNotIn("nigeria", result.lower())
    def test_extra_spaces(self):
        result = clean_address("DANGWAURO   ZARIA   ROAD")
        self.assertNotIn("  ", result)


class TestSplitConcatenated(unittest.TestCase):
    def test_all_caps_run_together(self):
        # Should insert space between digit and letter
        result = split_concatenated("BLOCK60COLLEGEROADALADJADELTA")
        self.assertIn(" ", result)
    def test_normal_address_unchanged(self):
        addr = "12 Broad Street, Lagos"
        result = split_concatenated(addr)
        # Should not mangle words — "Broad" should remain intact
        self.assertIn("Broad", result)


class TestKeywordLookup(unittest.TestCase):
    def test_gbagada(self):
        self.assertEqual(keyword_lga_lookup("1 Sunday Ogunyade Street, Gbagada Express Way"), "Kosofe")
    def test_warri(self):
        self.assertEqual(keyword_lga_lookup("118/120 AJAMIMOGHA ROAD, WARRI"), "Warri South")
    def test_owerri(self):
        self.assertEqual(keyword_lga_lookup("76 OKIGWE ROAD, OWERRI, IMO STATE"), "Owerri Municipal")
    def test_uselu(self):
        self.assertEqual(keyword_lga_lookup("LINE 5 SHOP 10 USELU MARKET EDO"), "Egor")
    def test_lekki(self):
        self.assertEqual(keyword_lga_lookup("NO 1 CHIEF HOPE HARRIMAN STREET LEKKI PHASE 1"), "Eti-Osa")
    def test_victoria_island_beats_island(self):
        # "victoria island" (longer) should match before "lagos island"
        self.assertEqual(keyword_lga_lookup("11B OKO AWO STREET, VICTORIA ISLAND"), "Eti-Osa")
    def test_ibadan(self):
        self.assertEqual(keyword_lga_lookup("Heritage Mall, Cocoa Road Dugbe Ibadan"), "Ibadan South West")
    def test_port_harcourt(self):
        self.assertEqual(keyword_lga_lookup("RIVERS STATE UNIVERSITY, PORT HARCOURT"), "Port Harcourt")
    def test_no_match(self):
        self.assertEqual(keyword_lga_lookup("123 Unknown Road, Somewhere"), "")
    def test_word_boundary_ojo(self):
        # "ojo" should not match inside "ojota"
        result = keyword_lga_lookup("Ojota Bus Stop Lagos")
        self.assertNotEqual(result, "Ojo")
    def test_benin_city_beats_benin(self):
        self.assertEqual(keyword_lga_lookup("80 NEW LAGOS ROAD BENIN CITY EDO"), "Oredo")


class TestExpandAbbreviations(unittest.TestCase):
    def test_ph_expands(self):
        result = expand_abbreviations("NO 4 EKE STREET RUMUOKUTA PH RIVER")
        self.assertIn("Port Harcourt", result)
    def test_phc_expands(self):
        result = expand_abbreviations("3 kingrekule GBRA PHC")
        self.assertIn("Port Harcourt", result)
    def test_vi_expands(self):
        result = expand_abbreviations("11B OKO AWO STREET VI LAGOS")
        self.assertIn("Victoria Island", result)
    def test_no_false_expansion(self):
        # "PHASE" should not be expanded
        result = expand_abbreviations("LEKKI PHASE 1")
        self.assertNotIn("Port Harcourt", result)


class TestCleanAddressV6(unittest.TestCase):
    def test_orphan_digits_stripped(self):
        result = clean_address("LEARNOFME STR PHASE1 EXTENSIO")
        # orphan "1" should be gone
        self.assertNotIn(" 1 ", f" {result} ")
    def test_phase_number_stripped(self):
        result = clean_address("LEKKI PHASE 1 LAGOS")
        self.assertNotIn("PHASE", result.upper())
    def test_shop_token_stripped(self):
        result = clean_address("SHOP 180 ADELABU COMPLEX ORITA")
        self.assertNotIn("180", result)


class TestSplitConcatenatedV6(unittest.TestCase):
    def test_road_suffix_split(self):
        result = split_concatenated("COLLEGEROADALADJA")
        self.assertIn(" ", result)
        self.assertIn("ROAD", result.upper())
    def test_street_suffix_split(self):
        result = split_concatenated("EKREGWARESTREET")
        self.assertIn(" ", result)
    def test_digit_letter_split(self):
        result = split_concatenated("BLOCK60COLLEGE")
        self.assertIn(" ", result)


class TestKeywordLookupV6(unittest.TestCase):
    def test_ph_abbreviation(self):
        # RUMUOKUTA is in Obio-Akpor — pass 1 correctly wins over PH expansion
        self.assertEqual(keyword_lga_lookup("NO 4 EKE STREET RUMUOKUTA PH RIVER"), "Obio-Akpor")

    def test_ph_only_address(self):
        # When no specific area keyword exists, PH expansion should fire
        self.assertEqual(keyword_lga_lookup("12 BROAD STREET PH"), "Port Harcourt")
    def test_phc_abbreviation(self):
        self.assertEqual(keyword_lga_lookup("3 kingrekule GBRA PHC"), "Port Harcourt")
    def test_yenegoa_typo(self):
        self.assertEqual(keyword_lga_lookup("5 MR BIGGS ROAD OPOLOYENYEN YENEGOA"), "Yenagoa")
    def test_mararaba_nasarawa(self):
        self.assertEqual(keyword_lga_lookup("SHOP NO 2 OPPOSITE PRINCE ALEX SCHOOL KABAYI MARARABA NASARAWA"), "Karu")
    def test_hadejia_jigawa(self):
        self.assertEqual(keyword_lga_lookup("2 KOFAR AREWA HADEJIA JIGAWA STATE"), "Hadejia")
    def test_nan_safe(self):
        import math
        self.assertEqual(keyword_lga_lookup(float("nan")), "")
        self.assertEqual(keyword_lga_lookup(None), "")
    def test_rumuadaolu_rivers(self):
        self.assertEqual(keyword_lga_lookup("NO 9 RUMUADAOLU STREET PHC"), "Obio-Akpor")


class TestInferStateV6(unittest.TestCase):
    def test_aminu_kano_crescent_no_kano(self):
        # "Aminu Kano Crescent" is a road name in Abuja — should NOT infer Kano
        result = infer_state_from_address("52, KUMASI CRESCENT, OFF AMINU KANO CRESCENT")
        self.assertNotEqual(result, "Kano")
    def test_mabushi_abuja(self):
        result = infer_state_from_address("PLOT 1497 CADASTRAL ZONE B06 MABUSHI DISTRICT ABUJA")
        self.assertEqual(result, "Federal Capital Territory")



    def test_includes_no_state_variant(self):
        variants = build_query_variants("12 Broad Street, Lagos Island", "Lagos")
        no_state = [v for v in variants if "Lagos, Nigeria" not in v and v.endswith(", Nigeria")]
        self.assertTrue(len(no_state) > 0, "Should include address-only (no state) variants")
    def test_deduplicates(self):
        variants = build_query_variants("Lekki", "Lagos")
        self.assertEqual(len(variants), len(set(v.lower() for v in variants)))
    def test_comma_free_uses_tokens(self):
        variants = build_query_variants("DANGWAURO ZARIA ROAD KANO", "Kano")
        # Should include token-based variants
        token_variants = [v for v in variants if "ZARIA" in v or "KANO" in v or "DANGWAURO" in v]
        self.assertTrue(len(token_variants) > 0)
    def test_state_only_fallback_present(self):
        variants = build_query_variants("Some Address", "Lagos")
        self.assertIn("Lagos, Nigeria", variants)


class TestExtractMeaningfulTokens(unittest.TestCase):
    def test_filters_stop_words(self):
        tokens = extract_meaningful_tokens("MINISTRY OF FINANCE KATSINA STATE SECRET")
        lower = [t.lower() for t in tokens]
        self.assertNotIn("state", lower)
        self.assertNotIn("ministry", lower)
    def test_returns_place_candidates(self):
        tokens = extract_meaningful_tokens("DANGWAURO ZARIA ROAD KANO")
        self.assertTrue(any(t.upper() in ("KANO", "ZARIA", "DANGWAURO") for t in tokens))


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]
    ))
    sys.exit(0 if result.wasSuccessful() else 1)
