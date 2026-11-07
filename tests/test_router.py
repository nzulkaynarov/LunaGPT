"""Unit tests for LunaGPT Smart Router without external dependencies."""

import unittest

from lunagpt.services.router import detect_criticism_and_model


class TestRouter(unittest.TestCase):
    def test_detect_model_general_and_code(self) -> None:
        prompt = "Напиши функцию на Python для парсинга json"
        model, reason, is_criticism = detect_criticism_and_model(prompt)
        self.assertEqual(model, "luna-smart")
        self.assertFalse(is_criticism)

    def test_detect_model_architecture(self) -> None:
        prompt = "Спроектируй event-driven архитектуру для микросервисов"
        model, reason, is_criticism = detect_criticism_and_model(prompt)
        self.assertEqual(model, "luna-pro")
        self.assertFalse(is_criticism)

    def test_detect_criticism_and_escalate(self) -> None:
        prompt = "Этот код не работает, выдает ошибку KeyError при парсинге"
        model, reason, is_criticism = detect_criticism_and_model(prompt, has_history=True)
        self.assertEqual(model, "luna-pro")
        self.assertTrue(is_criticism)
        self.assertIn("эскалация", reason.lower())

    def test_no_criticism_on_first_turn_even_with_error_word(self) -> None:
        prompt = "Как обработать ошибку KeyError в Python?"
        model, reason, is_criticism = detect_criticism_and_model(prompt, has_history=False)
        self.assertEqual(model, "luna-smart")
        self.assertFalse(is_criticism)

    def test_no_false_positive_on_substring_ne_to(self) -> None:
        prompt = "Мне только спросить про синтаксис декораторов"
        model, reason, is_criticism = detect_criticism_and_model(prompt, has_history=True)
        self.assertEqual(model, "luna-smart")
        self.assertFalse(is_criticism)

    def test_detect_openwebui_background_task(self) -> None:
        prompt = "### Task:\nGenerate a concise, 3-5 word title for the conversation."
        model, reason, is_criticism = detect_criticism_and_model(prompt)
        self.assertEqual(model, "luna-fast")
        self.assertFalse(is_criticism)

    def test_detect_model_fast(self) -> None:
        prompt = "Сделай краткую выжимку и переведи следующий текст"
        model, reason, is_criticism = detect_criticism_and_model(prompt)
        self.assertEqual(model, "luna-fast")
        self.assertFalse(is_criticism)


if __name__ == "__main__":
    unittest.main()
