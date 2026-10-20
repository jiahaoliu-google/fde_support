import os
import certifi
import google.auth
import google.auth.transport.requests
import requests

os.environ["SSL_CERT_FILE"] = certifi.where()

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "fde-support-sandbox-dev-jliu")
DATASTORE_ID = os.getenv("VERTEX_AGENT_SEARCH_DATASTORE_ID", "fde-support-faq-datastore-v2")

def query_datastore(search_query: str, page_size: int = 3):
    """
    Executes a direct search against the Vertex AI Search Datastore API.
    Prints matching document IDs, relevance ranking, customer questions, and brand responses.
    """
    print(f"\n============================================================")
    print(f" Datastore Search Query: '{search_query}'")
    print(f"============================================================")

    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)

    url = f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT_ID}/locations/global/collections/default_collection/dataStores/{DATASTORE_ID}/servingConfigs/default_search:search"

    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "X-Goog-User-Project": PROJECT_ID,
        "Content-Type": "application/json"
    }

    payload = {
        "query": search_query,
        "pageSize": page_size
    }

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        print(f"Error querying datastore (Status {resp.status_code}):", resp.text)
        return

    data = resp.json()
    results = data.get("results", [])
    total_size = data.get("totalSize", 0)

    print(f"Found {len(results)} matching records (Total datastore matches: {total_size}):\n")

    for i, res in enumerate(results, 1):
        doc = res.get("document", {})
        doc_id = doc.get("id", "N/A")
        struct_data = doc.get("structData", {})

        cust_q = struct_data.get("customer_question", "N/A")
        brand_resp = struct_data.get("reference_brand_response_text", "N/A")
        author = struct_data.get("brand_author", "N/A")

        print(f"[{i}] Document ID: {doc_id}")
        print(f"    Support Author  : @{author}")
        print(f"    Customer Query  : {cust_q}")
        print(f"    Brand Answer    : {brand_resp}")
        print("-" * 60)

if __name__ == "__main__":
    sample_queries = [
        "phone running very slow after updating to latest iOS",
        "Spotify skipping songs on Bluetooth speaker",
        "How to check my flight status"
    ]

    for q in sample_queries:
        query_datastore(q, page_size=2)
