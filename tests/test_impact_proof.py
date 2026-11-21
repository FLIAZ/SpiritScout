import unittest

from spiritscout.agents.impact import proof_status, validate_impact_proof


class ImpactProofTests(unittest.TestCase):
    def test_idor_requires_two_identities_and_controlled_object(self):
        proof = {
            "identity_a": "account-a",
            "identity_b": "account-b",
            "object_owner": "account-a",
            "control_ref": "exec-control",
            "variant_ref": "exec-variant",
            "impact": "account-b reads account-a order",
        }
        self.assertEqual(
            proof_status("idor", proof, execution_ids={"exec-control", "exec-variant"}),
            "impact_proven",
        )

    def test_idor_rejects_unknown_execution_reference(self):
        proof = {
            "identity_a": "account-a",
            "identity_b": "account-b",
            "object_owner": "account-a",
            "control_ref": "exec-control",
            "variant_ref": "forged",
        }
        self.assertIsNone(validate_impact_proof("idor", proof, execution_ids={"exec-control"}))
        self.assertIsNone(validate_impact_proof("idor", proof, execution_ids=set()))

    def test_xss_requires_browser_execution(self):
        base = {"execution_ref": "browser-1", "context": "html", "executed": True}
        self.assertEqual(proof_status("xss", base, execution_ids={"browser-1"}), "impact_proven")
        self.assertEqual(proof_status("xss", {**base, "executed": False}, execution_ids={"browser-1"}),
                         "technical_only")

    def test_ssrf_requires_callback_reference(self):
        self.assertEqual(proof_status("ssrf", {"callback_ref": "oast-1"}, execution_ids={"oast-1"}),
                         "impact_proven")
        self.assertEqual(proof_status("ssrf", {"impact": "reflected token"}, execution_ids={}),
                         "technical_only")
