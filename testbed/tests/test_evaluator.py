import unittest

from testbed.evaluator import EvaluationError, assert_result_matches_backend


class IndependentBalanceOracleTests(unittest.TestCase):
    def test_matches_pinned_parabank_positive_balance_semantics(self):
        result = {
            "status": "SUCCESS",
            "outputs": {"available_balance": "2450.75", "currency": "USD"},
        }
        backend_account = {"id": 101, "type": "SAVINGS", "balance": "2450.75"}

        assert_result_matches_backend(
            result, requested_account_id="101", backend_account=backend_account
        )

    def test_rejects_well_formed_but_wrong_balance(self):
        result = {
            "status": "SUCCESS",
            "outputs": {"available_balance": "2450.74", "currency": "USD"},
        }
        backend_account = {"id": 101, "type": "SAVINGS", "balance": "2450.75"}

        with self.assertRaises(EvaluationError):
            assert_result_matches_backend(
                result, requested_account_id="101", backend_account=backend_account
            )

    def test_negative_balance_has_zero_available_balance(self):
        result = {
            "status": "SUCCESS",
            "outputs": {"available_balance": "0.00", "currency": "USD"},
        }
        backend_account = {"id": 102, "type": "SAVINGS", "balance": "-12.50"}

        assert_result_matches_backend(
            result, requested_account_id="102", backend_account=backend_account
        )

        wrong_result = {
            "status": "SUCCESS",
            "outputs": {"available_balance": "-12.50", "currency": "USD"},
        }
        with self.assertRaises(EvaluationError):
            assert_result_matches_backend(
                wrong_result, requested_account_id="102", backend_account=backend_account
            )

    def test_rejects_non_string_or_non_canonical_money_outputs(self):
        for amount in (2450.75, "NaN", "Infinity", "1e2", "2450.750"):
            with self.subTest(amount_type=type(amount).__name__, amount=str(amount)):
                result = {
                    "status": "SUCCESS",
                    "outputs": {"available_balance": amount, "currency": "USD"},
                }
                with self.assertRaises(EvaluationError):
                    assert_result_matches_backend(
                        result, requested_account_id="101", backend_account={"id": 101, "type": "SAVINGS", "balance": "2450.75"}
                    )

    def test_rejects_account_identity_or_currency_mismatch(self):
        result = {
            "status": "SUCCESS",
            "outputs": {"available_balance": "2450.75", "currency": "USD"},
        }
        with self.assertRaises(EvaluationError):
            assert_result_matches_backend(
                result, requested_account_id="103", backend_account={"id": 101, "type": "SAVINGS", "balance": "2450.75"}
            )
        wrong_currency = {
            "status": "SUCCESS",
            "outputs": {"available_balance": "2450.75", "currency": "CAD"},
        }
        with self.assertRaises(EvaluationError):
            assert_result_matches_backend(
                wrong_currency, requested_account_id="101", backend_account={"id": 101, "type": "SAVINGS", "balance": "2450.75"}
            )

    def test_rejects_checking_account_even_when_balance_matches(self):
        result = {
            "status": "SUCCESS",
            "outputs": {"available_balance": "2450.75", "currency": "USD"},
        }
        with self.assertRaises(EvaluationError) as caught:
            assert_result_matches_backend(
                result,
                requested_account_id="101",
                backend_account={"id": 101, "type": "CHECKING", "balance": "2450.75"},
            )
        self.assertEqual(caught.exception.reason_code, "ACCOUNT_TYPE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
