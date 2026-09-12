/**
 * The one entry point for every call to this node's HTTP API.
 *
 * Every route except the dashboard's own assets and `/node_id` requires the
 * node's key, so a bare `fetch` gets a 401. Going through here attaches the key
 * and gives a single place to notice a refusal and raise the prompt for one.
 */

import { apiKeyStore } from "$lib/stores/apiKey.svelte";

const AUTHORIZATION_HEADER = "Authorization";

/**
 * Return `init` with the stored key attached as a bearer token.
 *
 * A header the caller set already wins, so a request that carries a deliberate
 * credential is never overwritten.
 */
function withApiKey(init?: RequestInit): RequestInit {
  const key = apiKeyStore.key;
  if (key === null) return init ?? {};
  const headers = new Headers(init?.headers);
  if (!headers.has(AUTHORIZATION_HEADER)) {
    headers.set(AUTHORIZATION_HEADER, `Bearer ${key}`);
  }
  return { ...init, headers };
}

/**
 * Call an API route with this browser's key attached.
 *
 * Returns the response as `fetch` would, refusals included: callers already
 * branch on `response.ok`, and a 401 is recorded here on the way past so the
 * prompt appears without every call site knowing about it.
 */
export async function apiFetch(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  const response = await fetch(input, withApiKey(init));
  if (response.status === 401) {
    apiKeyStore.markRequired();
  } else if (response.ok) {
    apiKeyStore.markAccepted();
  }
  return response;
}

/**
 * Report whether the node accepts `candidate` as its key.
 *
 * Sent to a cheap authenticated route so a mistyped key is rejected in the
 * prompt rather than stored and then failing on every later request. Deliberately
 * not `apiFetch`: a wrong candidate must not raise the prompt it is answering.
 */
export async function verifyApiKey(candidate: string): Promise<boolean> {
  try {
    const response = await fetch("/v1/feature-flags", {
      headers: { [AUTHORIZATION_HEADER]: `Bearer ${candidate}` },
    });
    return response.ok;
  } catch (error) {
    console.error("Failed to verify the API key:", error);
    return false;
  }
}
