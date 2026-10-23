/**
 * Redaction, on the client, before anything is queued.
 *
 * This mirrors the server's rules deliberately. Redacting in two places is not
 * redundancy for its own sake: the client copy means a secret is never written
 * to the local queue, so a device that is lost or inspected does not contain
 * the user's data. The server copy means a client that skipped it — an older
 * version, a third-party sender, a direct API call — is still covered.
 *
 * The rules must stay in step with `powerbase/redaction.py`. If you change one,
 * change the other.
 */

const PATTERNS: Array<[RegExp, string]> = [
  // Order matters. Aadhaar is twelve digits in four-four-four groups and would
  // otherwise be partly eaten by the phone rule below.
  [/\b\d{4}\s?\d{4}\s?\d{4}\b/g, "[REDACTED_AADHAAR]"],
  [/\b[A-Z]{5}\d{4}[A-Z]\b/g, "[REDACTED_PAN]"],
  [/\b(?:\d[ -]?){13,19}\b/g, "[REDACTED_CARD]"],
  [/\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, "[REDACTED_EMAIL]"],
  [/(?:\+?\d{1,3}[ -]?)?(?:\d[ -]?){9,12}\d\b/g, "[REDACTED_PHONE]"],
  // Tokens: bearer headers, JWTs, and the common key=value shapes.
  [/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [REDACTED_TOKEN]"],
  [/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g, "[REDACTED_JWT]"],
  [
    /\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|refresh[_-]?token|authorization|auth)\b\s*[:=]\s*("[^"]*"|'[^']*'|[^\s,;&]+)/gi,
    "$1=[REDACTED]",
  ],
];

/**
 * Names that mean "this is a credential". Matched against the whole key and
 * against the pieces a name is built from, so `apiKey`, `api_key`, `x-api-key`
 * and `user.password` are all caught while `sort_key` and `monkey` are not.
 */
const SENSITIVE_NAMES = new Set([
  "password", "passwd", "pwd", "passphrase", "secret", "clientsecret",
  "token", "accesstoken", "refreshtoken", "idtoken", "bearertoken", "csrftoken",
  "authorization", "authheader", "cookie", "cookies", "setcookie",
  "sessionkey", "sessiontoken", "privatekey", "publickey", "apikey",
  "apisecret", "secretkey", "signingkey", "credential", "credentials",
  "databaseurl", "dburl", "connectionstring", "dsn", "signedurl",
  "presignedurl", "cardnumber", "creditcard", "cvv", "cvc", "ssn", "aadhaar", "otp",
]);

export const MAX_VALUE_CHARS = 2048;
export const MAX_KEYS = 64;
export const MAX_DEPTH = 4;

export function isSensitiveName(key: string): boolean {
  if (!key) return false;
  const lowered = key.trim().toLowerCase();
  if (SENSITIVE_NAMES.has(lowered)) return true;

  const spaced = key.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
  const pieces = spaced.split(/[^a-z0-9]+/).filter(Boolean);
  if (pieces.some((piece) => SENSITIVE_NAMES.has(piece))) return true;

  const joined = pieces.join("");
  if (SENSITIVE_NAMES.has(joined)) return true;
  for (const name of SENSITIVE_NAMES) {
    if (joined.endsWith(name)) return true;
  }
  return false;
}

export function redactText(value: string): string {
  let out = value;
  for (const [pattern, replacement] of PATTERNS) {
    out = out.replace(pattern, replacement);
  }
  return out;
}

export interface SanitizedAttributes {
  attributes: Record<string, unknown>;
  /** How many keys were refused. Reported, never silent. */
  dropped: number;
}

/**
 * Drop credential-named keys and bound what a caller can make us store.
 *
 * The key is dropped whole, value and all: a field called `password` is a
 * credential whatever it contains.
 */
export function sanitizeAttributes(
  raw: unknown,
  maxKeys: number = MAX_KEYS,
  maxDepth: number = MAX_DEPTH,
): SanitizedAttributes {
  let dropped = 0;
  const DROP = Symbol("drop");

  const walk = (value: unknown, depth: number): unknown => {
    if (value === null || value === undefined) return value;

    if (Array.isArray(value)) {
      const items: unknown[] = [];
      for (let i = 0; i < value.length; i += 1) {
        if (i >= maxKeys) {
          dropped += 1;
          break;
        }
        const cleaned = walk(value[i], depth + 1);
        if (cleaned !== DROP) items.push(cleaned);
      }
      return items;
    }

    if (typeof value === "object") {
      if (depth >= maxDepth) {
        dropped += 1;
        return DROP;
      }
      const out: Record<string, unknown> = {};
      let kept = 0;
      for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
        if (isSensitiveName(key)) {
          dropped += 1;
          continue;
        }
        if (kept >= maxKeys) {
          dropped += 1;
          continue;
        }
        const cleaned = walk(item, depth + 1);
        if (cleaned !== DROP) {
          out[key] = cleaned;
          kept += 1;
        }
      }
      return out;
    }

    if (typeof value === "string") {
      const trimmed = value.length > MAX_VALUE_CHARS ? value.slice(0, MAX_VALUE_CHARS) : value;
      return redactText(trimmed);
    }
    if (typeof value === "number" || typeof value === "boolean") return value;
    return redactText(String(value)).slice(0, MAX_VALUE_CHARS);
  };

  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { attributes: {}, dropped: raw === undefined || raw === null ? 0 : 1 };
  }

  const result = walk(raw, 0);
  if (result === DROP || result === null || typeof result !== "object") {
    return { attributes: {}, dropped: dropped + 1 };
  }
  return { attributes: result as Record<string, unknown>, dropped };
}

/**
 * A stable, non-reversible name for a page, without the query string.
 *
 * Full URLs carry tokens, emails and ids in their query parameters, so they are
 * never sent. The origin and path are enough to say which page broke.
 */
export function safeLocation(href: string): string {
  try {
    const url = new URL(href);
    return `${url.origin}${url.pathname}`;
  } catch {
    return "";
  }
}
