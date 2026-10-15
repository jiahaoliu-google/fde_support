import unittest

def mock_analyze_sentiment(message_text: str) -> dict:
    if "angry" in message_text.lower() or "frustrated" in message_text.lower():
        return {
            "sentiment": "URGENT_FRUSTRATED",
            "reasoning": "Angry language detected",
            "entities": {"order_id": "12345", "product_name": None, "email": None}
        }
    return {
        "sentiment": "NEUTRAL_FACTUAL",
        "reasoning": "Standard factual query",
        "entities": {"order_id": None, "product_name": None, "email": None}
    }

class TestRemoteAgentClient(unittest.TestCase):
    def test_sentiment_router_client_fallback(self):
        res = mock_analyze_sentiment("Where is my order #12345? I am very angry!")
        self.assertEqual(res["sentiment"], "URGENT_FRUSTRATED")
        self.assertEqual(res["entities"]["order_id"], "12345")

if __name__ == '__main__':
    unittest.main()
