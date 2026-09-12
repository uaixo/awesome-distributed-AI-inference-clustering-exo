/**
 * ApiKeyStore - holds the node's API key for this browser and remembers it
 * across reloads.
 *
 * The key is the node's own shared secret rather than a per-user credential, so
 * one browser holds one key. It is kept in localStorage so a reload does not ask
 * for it again, and it only ever travels back to the node that served this page.
 */

import { browser } from "$app/environment";

const API_KEY_STORAGE_KEY = "exo-api-key";

class ApiKeyStore {
  key = $state<string | null>(null);

  /**
   * Whether the node has refused a request for want of a key.
   *
   * Set from the one place that sees every response, so a 401 anywhere raises
   * the prompt and polling stops rather than failing once a second in silence.
   */
  isRequired = $state(false);

  constructor() {
    if (browser) {
      this.loadFromStorage();
    }
  }

  private loadFromStorage() {
    try {
      this.key = localStorage.getItem(API_KEY_STORAGE_KEY);
    } catch (error) {
      console.error("Failed to load the API key:", error);
    }
  }

  /** Store `key` as this browser's key and drop the prompt. */
  set(key: string) {
    this.key = key;
    this.isRequired = false;
    try {
      localStorage.setItem(API_KEY_STORAGE_KEY, key);
    } catch (error) {
      console.error("Failed to save the API key:", error);
    }
  }

  /** Forget the stored key, so the next refused request prompts for a new one. */
  clear() {
    this.key = null;
    try {
      localStorage.removeItem(API_KEY_STORAGE_KEY);
    } catch (error) {
      console.error("Failed to clear the API key:", error);
    }
  }

  /**
   * Record that the node refused a request.
   *
   * The stored key is kept rather than cleared: a user who mistypes it once sees
   * it in the prompt to correct, and a node restarted with a different key is
   * the same prompt either way.
   */
  markRequired() {
    this.isRequired = true;
  }

  /** Record that the node served a request, so any prompt on screen can close. */
  markAccepted() {
    this.isRequired = false;
  }
}

export const apiKeyStore = new ApiKeyStore();
