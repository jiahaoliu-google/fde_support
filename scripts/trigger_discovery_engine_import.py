#!/usr/bin/env python3
import json
import google.auth
from google.auth.transport.requests import Request
import requests

def trigger_import():
    project_id = "fde-support-sandbox-dev-jliu"
    location = "global"
    datastore_id = "fde-support-faq-datastore"
    gcs_uri = f"gs://fde-support-faq-docs-{project_id}/twcs.jsonl"

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())

    url = f"https://discoveryengine.googleapis.com/v1/projects/{project_id}/locations/{location}/dataStores/{datastore_id}/branches/0/documents:import"
    
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json"
    }

    payload = {
        "gcsSource": {
            "inputUris": [gcs_uri],
            "dataSchema": "document"
        },
        "reconciliationMode": "INCREMENTAL"
    }

    print(f"Triggering Discovery Engine document import for {gcs_uri}...")
    response = requests.post(url, headers=headers, json=payload)
    
    print(f"HTTP Status: {response.status_code}")
    print("Response JSON:")
    print(json.dumps(response.json(), indent=2))

if __name__ == "__main__":
    trigger_import()
