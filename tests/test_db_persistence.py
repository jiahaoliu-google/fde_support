import unittest
import sqlite3

class TestDBPersistence(unittest.TestCase):
    def test_sqlite_ticket_persistence(self):
        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE tickets (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                current_status TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                assigned_queue TEXT NOT NULL,
                order_id TEXT
            )
        """)

        cursor.execute("""
            INSERT INTO tickets (id, customer_id, current_status, sentiment, assigned_queue, order_id)
            VALUES ('t-1', 'cust_123', 'Active', 'NEUTRAL_FACTUAL', 'Automated RAG Queue', '998877')
        """)
        conn.commit()

        cursor.execute("SELECT customer_id, current_status, sentiment, order_id FROM tickets WHERE id='t-1'")
        row = cursor.fetchone()
        
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "cust_123")
        self.assertEqual(row[1], "Active")
        self.assertEqual(row[2], "NEUTRAL_FACTUAL")
        self.assertEqual(row[3], "998877")

if __name__ == '__main__':
    unittest.main()
