"""Unit tests for LunaGPT Smart Router without external dependencies."""

import unittest
from lunagpt.services.router import detect_criticism_and_model


class TestRouter(unittest.TestCase):
    def test_detect_model_normal_code(self) -> None:
        prompt = "Напиши функцию на Python для парсинга json"
        model, reason, is_criticism = detect_criticism_and_model(prompt)
        self.assertEqual(model, "luna-coder")
        self.assertFalse(is_criticism)

    def test_detect_model_architecture(self) -> None:
        prompt = "Спроектируй event-driven архитектуру для микросервисов"
        model, reason, is_criticism = detect_criticism_and_model(prompt)
        self.assertEqual(model, "luna-smart")
        self.assertFalse(is_criticism)

    def test_detect_criticism_and_escalate(self) -> None:
        prompt = "Этот код не работает, выдает ошибку KeyError при парсинге"
        model, reason, is_criticism = detect_criticism_and_model(prompt)
        self.assertEqual(model, "luna-smart")
        self.assertTrue(is_criticism)
        self.assertIn("эскалация", reason.lower())


if __name__ == "__main__":
    unittest.main()
