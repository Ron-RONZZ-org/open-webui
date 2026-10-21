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
	it('surfaces execute_code files and files_warning from the JSON tool result', () => {
		const files = [
			{
				id: 'f1876364',
				name: 'generated-image.svg',
				path: '/app/backend/data/uploads/f1876364_generated-image.svg',
				url: '/api/v1/files/f1876364/content'
			}
		];
		const filesWarning = 'chart.svg: could not be attached to the chat.';

		const items = buildOutputDisplayItems(
			executeCodeOutput({ status: 'success', files, files_warning: filesWarning })
		);

		expect(items).toHaveLength(1);
		const item = items[0];
		if (item.type !== 'detail_single') {
			throw new Error(`expected detail_single, got ${item.type}`);
		}
		expect(item.token.attributes.name).toBe('execute_code');
		expect(decode(String(item.token.attributes.files))).toBe(JSON.stringify(files));
		expect(decode(String(item.token.attributes.filesWarning))).toBe(filesWarning);
	});

	it('omits filesWarning when the tool result has no warning', () => {
		const items = buildOutputDisplayItems(executeCodeOutput({ status: 'success', stdout: 'ok' }));

		const item = items[0];
		if (item.type !== 'detail_single') {
			throw new Error(`expected detail_single, got ${item.type}`);
		}
		expect(item.token.attributes.filesWarning).toBeUndefined();
	});

	it('hides the legacy model-only files_note from the displayed tool result', () => {
		const files = [
			{
				id: 'f1876364',
				name: 'generated-image.svg',
				path: '/app/backend/data/uploads/f1876364_generated-image.svg',
				url: '/api/v1/files/f1876364/content'
			}
		];
		const note =
			'The staged files are attached to this chat and visible to the user. Each path in files[] is the file on the server; pass it to a file-upload tool — do not re-encode or re-send the bytes.';

		const items = buildOutputDisplayItems(
			executeCodeOutput({ status: 'success', files, files_note: note })
		);

		const item = items[0];
		if (item.type !== 'detail_single') {
			throw new Error(`expected detail_single, got ${item.type}`);
		}
		expect(decode(String(item.token.attributes.files))).toBe(JSON.stringify(files));
		expect(item.token.attributes.filesWarning).toBeUndefined();
		expect(item.token.text).not.toContain('files_note');
		expect(item.token.text).not.toContain('do not re-encode');
		expect(item.token.text).toContain('"files"');
	});
});
