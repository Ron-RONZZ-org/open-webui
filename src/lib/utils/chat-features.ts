/**
 * Feature flags as the request carries them, keyed by the names the backend
 * reads under `metadata.features`.
 */
export type ChatFeatureFlags = Record<string, boolean | undefined>;

/**
 * Merge the feature flags captured when a message was queued into the flags of
 * the request that finally sends it. The queue is a promise about the message,
 * not about the chat state at the moment the queue drains: if the code
 * interpreter toggle was on when the user queued the message, the queued send
 * keeps it on even when a chat reload in between reset the toggle.
 *
 * Keys the snapshot does not carry keep their current value, so a future
 * feature cannot be lost by an old queue item.
 */
export function mergeFeatureFlags(
	current: ChatFeatureFlags,
	override?: ChatFeatureFlags | null
): ChatFeatureFlags {
	return override ? { ...current, ...override } : current;
}
