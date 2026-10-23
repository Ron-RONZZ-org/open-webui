import { describe, expect, it } from 'vitest';
import { mergeFeatureFlags } from './chat-features';

describe('mergeFeatureFlags', () => {
	it('keeps the current flags when no snapshot was taken', () => {
		const current = { code_interpreter: true, web_search: false };
		expect(mergeFeatureFlags(current)).toBe(current);
		expect(mergeFeatureFlags(current, null)).toBe(current);
	});

	it('lets the queued snapshot win over a toggle that changed afterwards', () => {
		const current = { code_interpreter: false, web_search: true, image_generation: false };
		const queued = { code_interpreter: true, web_search: false };
		expect(mergeFeatureFlags(current, queued)).toEqual({
			code_interpreter: true,
			web_search: false,
			image_generation: false
		});
	});

	it('does not drop a flag the snapshot predates', () => {
		const current = { code_interpreter: true, image_generation: true };
		expect(mergeFeatureFlags(current, { code_interpreter: false })).toEqual({
			code_interpreter: false,
			image_generation: true
		});
	});
});
