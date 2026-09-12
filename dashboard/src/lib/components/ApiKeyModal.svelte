<script lang="ts">
  import { fade, fly } from "svelte/transition";
  import { cubicOut } from "svelte/easing";
  import { verifyApiKey } from "$lib/api";
  import { apiKeyStore } from "$lib/stores/apiKey.svelte";
  import { refreshAfterAuthentication } from "$lib/stores/app.svelte";

  const isRequired = $derived(apiKeyStore.isRequired);

  let candidate = $state("");
  let isVerifying = $state(false);
  let wasRejected = $state(false);

  /** Seed the field with the stored key so a mistyped one can be corrected. */
  $effect(() => {
    if (isRequired && candidate === "") {
      candidate = apiKeyStore.key ?? "";
    }
  });

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const trimmed = candidate.trim();
    if (trimmed === "" || isVerifying) return;

    isVerifying = true;
    wasRejected = false;
    const accepted = await verifyApiKey(trimmed);
    isVerifying = false;

    if (accepted) {
      apiKeyStore.set(trimmed);
      candidate = "";
      refreshAfterAuthentication();
    } else {
      wasRejected = true;
    }
  }
</script>

{#if isRequired}
  <!-- No dismiss: every panel on the page is empty until the node accepts a key. -->
  <div
    class="fixed inset-0 z-[100] bg-black/80 backdrop-blur-sm"
    transition:fade={{ duration: 200 }}
    role="presentation"
  ></div>

  <div
    class="fixed z-[100] top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(90vw,440px)] bg-exo-dark-gray border border-exo-yellow/10 rounded-lg shadow-2xl overflow-hidden"
    transition:fly={{ y: 20, duration: 300, easing: cubicOut }}
    role="dialog"
    aria-modal="true"
    aria-labelledby="api-key-modal-title"
  >
    <div class="p-4 border-b border-exo-yellow/10 bg-exo-medium-gray/30">
      <h2
        id="api-key-modal-title"
        class="text-xs font-mono tracking-wider uppercase text-foreground"
      >
        API key required
      </h2>
    </div>

    <form class="flex flex-col gap-3 p-4" onsubmit={submit}>
      <p class="text-xs text-muted-foreground leading-relaxed">
        This node requires its API key. It is printed in the exo startup log and
        stored in the node's cache directory &mdash;
        <code class="font-mono text-foreground">~/.exo/api_key</code> on macOS,
        <code class="font-mono text-foreground">~/.cache/exo/api_key</code> on Linux.
      </p>

      <input
        type="password"
        bind:value={candidate}
        placeholder="Paste the key"
        autocomplete="off"
        spellcheck="false"
        aria-label="API key"
        aria-invalid={wasRejected}
        class="w-full px-3 py-2 font-mono text-sm bg-exo-medium-gray/40 border border-exo-yellow/10 rounded focus:outline-none focus:border-exo-yellow/40 text-foreground placeholder:text-muted-foreground"
      />

      {#if wasRejected}
        <p class="text-xs font-mono text-red-400" role="alert">
          The node refused that key.
        </p>
      {/if}

      <button
        type="submit"
        disabled={candidate.trim() === "" || isVerifying}
        class="px-3 py-2 text-xs font-mono tracking-wider uppercase bg-exo-medium-gray/60 border border-exo-yellow/20 rounded hover:border-exo-yellow/50 disabled:opacity-40 disabled:hover:border-exo-yellow/20 text-foreground"
      >
        {isVerifying ? "Checking…" : "Connect"}
      </button>
    </form>
  </div>
{/if}
