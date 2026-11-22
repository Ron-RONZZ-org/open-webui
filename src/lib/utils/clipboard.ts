const KATEX_SELECTOR = '.katex';
const KATEX_HTML_SELECTOR = '.katex-html';
const KATEX_DISPLAY_SELECTOR = '.katex-display';
const TEX_ANNOTATION_SELECTOR = 'annotation[encoding="application/x-tex"]';
const EDITABLE_SELECTOR =
	'input, textarea, [contenteditable]:not([contenteditable="false"]), .cm-editor';

export interface SelectionCopyPayload {
	text: string;
	html: string;
}

function elementFor(node: Node | null): Element | null {
	if (!node) {
		return null;
	}
	return node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
}

function katexAncestor(node: Node | null): Element | null {
	return elementFor(node)?.closest(KATEX_SELECTOR) ?? null;
}

function katexSource(node: Element): string | null {
	const source = node.querySelector(TEX_ANNOTATION_SELECTOR)?.textContent;
	return source && source.trim().length > 0 ? source : null;
}

/**
 * Replace each rendered KaTeX node in a cloned fragment with its TeX source so
 * the plain-text clipboard contains `$...$` / `$$...$$` instead of the two
 * rendered representations (MathML + HTML).
 */
export function replaceRenderedMathWithTex(root: ParentNode): void {
	root.querySelectorAll(KATEX_SELECTOR).forEach((node) => {
		const source = katexSource(node);
		if (source === null) {
			return;
		}

		const displayMode = node.closest(KATEX_DISPLAY_SELECTOR) !== null;
		node.replaceWith(document.createTextNode(displayMode ? `$$${source}$$` : `$${source}$`));
	});
}

/**
 * Drop the visual KaTeX markup from the HTML clipboard, keeping the MathML.
 * Without it a rich-text target renders both the MathML and the HTML copy.
 */
export function removeRenderedMathHtml(root: ParentNode): void {
	root.querySelectorAll(KATEX_HTML_SELECTOR).forEach((node) => node.remove());
}

export function isEditableTarget(target: EventTarget | null): boolean {
	return target instanceof Element && target.closest(EDITABLE_SELECTOR) !== null;
}

export function isEditableSelection(selection: Selection): boolean {
	return elementFor(selection.anchorNode)?.closest(EDITABLE_SELECTOR) !== null;
}

/**
 * A selection that starts or ends inside a formula only clones part of the
 * KaTeX markup. Widen it to the formula root so the copied text keeps the
 * whole formula instead of a fragment.
 */
function expandPartialMath(range: Range): Range {
	const start = katexAncestor(range.startContainer);
	const end = katexAncestor(range.endContainer);
	if (!start && !end) {
		return range;
	}

	const expanded = range.cloneRange();
	if (start) {
		expanded.setStartBefore(start);
	}
	if (end) {
		expanded.setEndAfter(end);
	}
	return expanded;
}

function rangeHasMath(selection: Selection): boolean {
	if (selection.rangeCount === 0) {
		return false;
	}

	const range = selection.getRangeAt(0);
	if (katexAncestor(range.startContainer) || katexAncestor(range.endContainer)) {
		return true;
	}
	return range.cloneContents().querySelector(KATEX_SELECTOR) !== null;
}

function applyTableCopyStyles(root: ParentNode): void {
	root.querySelectorAll('table').forEach((table) => {
		table.style.borderCollapse = 'collapse';
		table.style.width = 'auto';
		table.style.tableLayout = 'auto';
	});

	root.querySelectorAll('th').forEach((th) => {
		th.style.whiteSpace = 'nowrap';
		th.style.padding = '4px 8px';
	});
}

/**
 * `innerText` needs layout, so the fragment is attached offscreen. Without it
 * (e.g. in a DOM test environment) fall back to `textContent`.
 */
function innerTextOf(node: HTMLElement): string {
	const host = document.createElement('div');
	host.setAttribute('aria-hidden', 'true');
	host.style.cssText = 'position:fixed;left:-10000px;top:0;white-space:pre-wrap;';
	host.appendChild(node);
	document.body.appendChild(host);

	try {
		return typeof host.innerText === 'string' ? host.innerText : (host.textContent ?? '');
	} finally {
		host.remove();
	}
}

function cleanPlainText(text: string): string {
	return text
		.replace(/\r\n/g, '\n')
		.replace(/^[ \t]+$/gm, '')
		.replace(/\n{3,}/g, '\n\n')
		.replace(/^\n+|\n+$/g, '');
}

export function buildSelectionCopyPayload(selection: Selection): SelectionCopyPayload | null {
	if (selection.isCollapsed || selection.rangeCount === 0) {
		return null;
	}

	const range = expandPartialMath(selection.getRangeAt(0));
	const source = document.createElement('div');
	source.appendChild(range.cloneContents());
	applyTableCopyStyles(source);

	const rich = source.cloneNode(true) as HTMLElement;
	removeRenderedMathHtml(rich);

	let text: string;
	if (rangeHasMath(selection)) {
		replaceRenderedMathWithTex(source);
		text = innerTextOf(source);
	} else {
		// Preserve the pre-existing behaviour for selections without math.
		text = selection.toString();
	}

	return { text: cleanPlainText(text), html: rich.innerHTML };
}

/**
 * App-wide fallback for selections the per-message copy handler cannot see:
 * user messages, the merged view and selections that span several messages.
 * Selections without rendered math are left to the browser.
 */
export function installSelectionCopyHandler(): () => void {
	const onCopy = (event: ClipboardEvent) => {
		if (event.defaultPrevented || !event.clipboardData) {
			return;
		}
		if (isEditableTarget(event.target)) {
			return;
		}

		const selection = window.getSelection();
		if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
			return;
		}
		if (isEditableSelection(selection) || !rangeHasMath(selection)) {
			return;
		}

		const payload = buildSelectionCopyPayload(selection);
		if (!payload) {
			return;
		}

		event.clipboardData.setData('text/html', payload.html);
		event.clipboardData.setData('text/plain', payload.text);
		event.preventDefault();
	};

	document.addEventListener('copy', onCopy);
	return () => document.removeEventListener('copy', onCopy);
}
