<script lang="ts" context="module">
	import type { renderToString as katexRenderToString } from 'katex';

	// Module-level singleton: load katex once, share across all KatexRenderer instances
	let katexRenderer: Promise<typeof katexRenderToString> | null = null;
	function getKatexRenderer(): Promise<typeof katexRenderToString> {
		if (!katexRenderer) {
			katexRenderer = Promise.all([
				import('katex'),
				import('katex/contrib/mhchem'),
				import('katex/dist/katex.min.css')
			]).then(([katex]) => katex.renderToString);
		}
		return katexRenderer;
	}
</script>

<script lang="ts">
	import { onMount, getContext } from 'svelte';
	import { copyToClipboard } from '$lib/utils';
	import { toast } from 'svelte-sonner';

	import Clipboard from '$lib/components/icons/Clipboard.svelte';

	const i18n = getContext('i18n');

	export let content: string;
	export let displayMode: boolean = false;

	let renderToString: typeof katexRenderToString | null = null;

	onMount(async () => {
		renderToString = await getKatexRenderer();
	});

	const copy = async () => {
		if (await copyToClipboard(content)) {
			toast.success($i18n.t('Copied to clipboard'));
		}
	};

	// Cache rendered HTML — only re-compute when content, displayMode, or renderToString changes,
	// not on every parent re-render during streaming
	let renderedHTML: string = '';
	$: if (renderToString) {
		try {
			renderedHTML = renderToString(content, { displayMode, throwOnError: false });
		} catch {
			// throwOnError only suppresses ParseError; RangeError on deep nesting escapes, so escape the fallback (never {@html} raw source)
			renderedHTML = content
				.replaceAll('&', '&amp;')
				.replaceAll('<', '&lt;')
				.replaceAll('>', '&gt;');
		}
	}
</script>

{#if renderToString && renderedHTML}
	<!-- svelte-ignore a11y-click-events-have-key-events -->
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<svelte:element
		this={displayMode ? 'div' : 'span'}
		class="cursor-pointer {displayMode ? 'group relative' : ''}"
		title={$i18n.t('Copy LaTeX')}
		on:click={copy}
	>
		{@html renderedHTML}
		{#if displayMode}
			<button
				type="button"
				class="hover-reveal absolute right-1 top-1 z-20 rounded-lg bg-white/80 p-1 text-gray-500 transition hover:bg-gray-50 dark:bg-gray-850/80 dark:text-gray-300 dark:hover:bg-gray-800"
				title={$i18n.t('Copy LaTeX')}
				aria-label={$i18n.t('Copy LaTeX')}
				on:click|stopPropagation={copy}
			>
				<Clipboard className="size-3.5" strokeWidth="1.5" />
			</button>
		{/if}
	</svelte:element>
{/if}
