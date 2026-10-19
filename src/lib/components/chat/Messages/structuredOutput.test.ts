import { describe, expect, it } from 'vitest';
import { decode } from 'html-entities';
import { buildOutputDisplayItems, type OutputItem } from './structuredOutput';

const executeCodeOutput = (result: Record<string, unknown>): OutputItem[] => [
	{
		type: 'function_call',
		id: 'call_1',
		call_id: 'call_1',
		name: 'execute_code',
		arguments: '{}',
		status: 'completed'
	},
	{
		type: 'function_call_output',
		id: 'out_1',
		call_id: 'call_1',
		status: 'completed',
		output: [{ type: 'input_text', text: JSON.stringify(result) }]
	}
];

describe('buildOutputDisplayItems', () => {
	it('surfaces execute_code files and files_note from the JSON tool result', () => {
		const files = [
			{
				id: 'f1876364',
				name: 'generated-image.svg',
				path: '/app/backend/data/uploads/f1876364_generated-image.svg',
				url: '/api/v1/files/f1876364/content'
			}
		];
		const filesNote = 'Images in files[] are already uploaded.';

		const items = buildOutputDisplayItems(
			executeCodeOutput({ status: 'success', files, files_note: filesNote })
		);

		expect(items).toHaveLength(1);
		const item = items[0];
		if (item.type !== 'detail_single') {
			throw new Error(`expected detail_single, got ${item.type}`);
		}
		expect(item.token.attributes.name).toBe('execute_code');
		expect(decode(String(item.token.attributes.files))).toBe(JSON.stringify(files));
		expect(decode(String(item.token.attributes.filesNote))).toBe(filesNote);
	});

	it('omits filesNote when the tool result has no note', () => {
		const items = buildOutputDisplayItems(executeCodeOutput({ status: 'success', stdout: 'ok' }));

		const item = items[0];
		if (item.type !== 'detail_single') {
			throw new Error(`expected detail_single, got ${item.type}`);
		}
		expect(item.token.attributes.filesNote).toBeUndefined();
	});
});
