"""Parsing of Agent Runtime responses: NDJSON stream text, citations, search-tool errors,
and the FAQ answer payload."""
import json
import re

def parse_ndjson_stream(raw_text: str) -> tuple[str, list[str]]:
    """Parses NDJSON streamed response from Reasoning Engine endpoints."""
    full_text = ""
    citations = []
    
    for line in raw_text.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if "content" in data and "parts" in data["content"]:
                event_text = ""
                for part in data["content"]["parts"]:
                    if part.get("thought"):
                        continue
                    event_text += part.get("text", "") or ""

                    # Citations from FunctionTool responses (search_support_records / get_order_status)
                    fr = part.get("function_response") or part.get("functionResponse") or {}
                    fr_resp = fr.get("response") or {}
                    if fr.get("name") == "search_support_records":
                        for idx, rec in enumerate(fr_resp.get("results") or [], 1):
                            doc_id = rec.get("doc_id") if isinstance(rec, dict) else None
                            if doc_id:
                                c_entry = f"Document #{idx} (ID: {doc_id})"
                                if c_entry not in citations:
                                    citations.append(c_entry)
                        if fr_resp.get("query"):
                            c_entry = f"Datastore Search: {fr_resp['query']}"
                            if c_entry not in citations:
                                citations.append(c_entry)
                    elif fr.get("name") == "get_order_status" and fr_resp.get("status") == "found":
                        oid = (fr_resp.get("order") or {}).get("order_id")
                        if oid and f"Order Lookup: {oid}" not in citations:
                            citations.append(f"Order Lookup: {oid}")

                # Keep only the latest model text: earlier events may contain pre-tool-call text
                if event_text.strip():
                    full_text = event_text
            
            # Extract grounding metadata citations
            gm = data.get("grounding_metadata") or data.get("groundingMetadata") or {}
            if gm:
                chunks = gm.get("grounding_chunks") or gm.get("groundingChunks") or []
                for idx, chunk in enumerate(chunks, 1):
                    rc = chunk.get("retrieved_context") or chunk.get("retrievedContext") or {}
                    web = chunk.get("web") or {}
                    doc_path = (
                        rc.get("document")
                        or rc.get("document_name")
                        or rc.get("documentName")
                        or rc.get("uri")
                        or rc.get("title")
                        or web.get("title")
                        or web.get("uri")
                    )
                    if doc_path:
                        # Combine Document Ref index and Document ID into single record
                        if "/documents/" in str(doc_path):
                            doc_id = str(doc_path).split("/documents/")[-1]
                            c_entry = f"Document #{idx} (ID: {doc_id})"
                        else:
                            c_entry = f"Document #{idx} (ID: {doc_path})"
                    else:
                        c_entry = f"Document #{idx}"
                    if c_entry not in citations:
                        citations.append(c_entry)
                
                ret_queries = gm.get("retrieval_queries") or gm.get("retrievalQueries") or []
                for q in ret_queries:
                    c_entry = f"Datastore Search: {q}"
                    if c_entry not in citations:
                        citations.append(c_entry)
                        
                web_queries = gm.get("web_search_queries") or gm.get("webSearchQueries") or []
                for q in web_queries:
                    c_entry = f"Web Search: {q}"
                    if c_entry not in citations:
                        citations.append(c_entry)
        except Exception:
            pass

    return full_text, citations

def format_citations(raw_citations: list[str]) -> list[str]:
    """Normalizes raw citation references into descriptive Document Names/IDs for UI rendering."""
    formatted = []
    for c in raw_citations:
        c_str = str(c).strip()
        if not c_str:
            continue
        if "(ID:" in c_str and not ("/documents/" in c_str or ": projects/" in c_str):
            entry = c_str
        elif "/documents/" in c_str:
            doc_id = c_str.split("/documents/")[-1].strip()
            if c_str.startswith("Document #") or c_str.startswith("Document#"):
                doc_num = c_str.split(":")[0].strip()
                entry = f"{doc_num} (ID: {doc_id})"
            elif c_str.startswith("Datastore Document Ref #"):
                ref = c_str.split(":")[0].replace("Datastore Document Ref #", "").strip()
                entry = f"Document #{ref} (ID: {doc_id})"
            else:
                entry = f"Document (ID: {doc_id})"
        elif c_str.startswith("Datastore Document Ref #"):
            ref = c_str.replace("Datastore Document Ref #", "").strip()
            entry = f"Document #{ref}"
        elif c_str.startswith("Datastore Doc ID:"):
            doc_id = c_str.replace("Datastore Doc ID:", "").strip()
            entry = f"Document (ID: {doc_id})"
        elif c_str.isdigit():
            entry = f"Document #{c_str}"
        elif c_str.startswith("http://") or c_str.startswith("https://") or c_str.startswith("gs://"):
            fname = c_str.split("/")[-1] or c_str
            entry = f"Doc: {fname} ({c_str})"
        else:
            entry = c_str

        if entry not in formatted:
            formatted.append(entry)
    return formatted

# search_support_records error types (agents/faq_assistant/tools.py) that mean Agent Search
# itself is unhealthy, mapped to the HTTP status the circuit breaker should record.
# auth_error / permission_denied are configuration faults that won't recover on their own,
# so they are left to the agent's normal escalate=true answer and don't trip the breaker.
SEARCH_TOOL_ERROR_STATUS = {"rate_limited": 429, "search_unavailable": 503}

def detect_search_tool_failure(raw_text: str) -> tuple[int, str] | None:
    """Returns (status_code, error_type) if Agent Search failed during this agent turn.

    Reads the search_support_records function_response events in the NDJSON stream.
    Returns None if no search ran, if any search in the turn succeeded (the answer can
    still be grounded), or if the only errors are non-transient ones. A 429 wins over
    a 503 when both appear.
    """
    failure = None
    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        content = data.get("content") if isinstance(data, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            fr = part.get("function_response") or part.get("functionResponse")
            if not isinstance(fr, dict) or fr.get("name") != "search_support_records":
                continue
            resp = fr.get("response")
            status = resp.get("status") if isinstance(resp, dict) else None
            if status in ("success", "no_results"):
                return None
            if status == "error":
                error_type = resp.get("error_type")
                code = SEARCH_TOOL_ERROR_STATUS.get(error_type)
                if code and (failure is None or code == 429):
                    failure = (code, error_type)
    return failure

def unwrap_faq_answer(faq_data: dict) -> tuple[str, list, bool, bool]:
    """Returns (answer, citations, grounded, escalate) from a call_faq_agent result.

    The model sometimes puts its whole JSON reply (optionally in a ```json fence) inside
    the answer string. If so, the inner answer is used, inner citations are merged in, and
    inner grounded/escalate flags override the outer ones. If the inner JSON is malformed,
    a regex salvages the answer text.
    """
    answer_text = faq_data.get("answer", "No answer generated.")
    citations = faq_data.get("citations", [])
    grounded = faq_data.get("grounded", True)
    escalate = bool(faq_data.get("escalate", False))

    if isinstance(answer_text, str):
        raw_str = answer_text.strip()
        if raw_str.startswith("```json"):
            raw_str = raw_str.split("```json")[-1].split("```")[0].strip()
        elif raw_str.startswith("```"):
            raw_str = raw_str.split("```")[1].strip()

        if raw_str.startswith("{") and raw_str.endswith("}"):
            try:
                parsed = json.loads(raw_str, strict=False)
                if isinstance(parsed, dict) and "answer" in parsed:
                    answer_text = parsed["answer"]
                    if "citations" in parsed and isinstance(parsed["citations"], list):
                        for c in parsed["citations"]:
                            if str(c) not in citations:
                                citations.append(str(c))
                    if "grounded" in parsed:
                        grounded = bool(parsed["grounded"])
                    if "escalate" in parsed:
                        escalate = bool(parsed["escalate"])
            except Exception:
                match = re.search(r'"answer":\s*"(.*?)"\s*,\s*"(?:citations|grounded)"', raw_str, re.DOTALL)
                if match:
                    answer_text = match.group(1).replace("\\n", "\n").replace('\\"', '"')

    return answer_text, citations, grounded, escalate
