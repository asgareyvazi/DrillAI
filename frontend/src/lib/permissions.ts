/**
 * Permission patterns, as the platform's RBAC defines them.
 *
 * A role holds *patterns*, not permissions: `workflow.*` covers `workflow.read` and
 * `workflow.publish`, `workflow.**` covers everything below `workflow`, `**` covers everything. The
 * server evaluates these on every request — this module exists so the interface does not offer an
 * action that is certain to be refused ("a publish button that always fails is worse than no
 * button", as the identity endpoint puts it), and for no other purpose. Nothing here grants
 * anything, and no route is protected by it.
 *
 * The rules mirror `drillai.security.rbac._pattern_matches` case for case, including the distinction
 * between `workflow.*` (exactly one more segment) and `workflow.**` (any depth). `fnmatch` in the
 * server expands `?` and `[...]`; the catalogue uses neither, so the glob here covers `*` and `?`
 * and treats a bracket literally rather than pretending to an equivalence it does not implement.
 * The agreement is not left to this comment: an end-to-end journey switches identity and asserts
 * that the offered actions match what the server accepts.
 */

/** Python's `fnmatch.fnmatchcase` for the subset of patterns the platform defines. */
function globMatches(pattern: string, value: string): boolean {
  let expression = ''
  for (const character of pattern) {
    if (character === '*') expression += '.*'
    else if (character === '?') expression += '.'
    else expression += character.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  }
  return new RegExp(`^${expression}$`, 's').test(value)
}

/** Does a single pattern cover a permission? */
export function patternMatches(pattern: string, permission: string): boolean {
  if (pattern === permission) return true
  if (pattern.endsWith('.**')) return permission.startsWith(`${pattern.slice(0, -3)}.`)
  if (pattern.endsWith('.*')) {
    const prefix = pattern.slice(0, -2)
    return permission.split('.').length === prefix.split('.').length + 1 && permission.startsWith(`${prefix}.`)
  }
  if (pattern.includes('*')) return globMatches(pattern, permission)
  return false
}

/** Does any of the identity's patterns cover the permission an action requires? */
export function holdsPermission(patterns: readonly string[] | null | undefined, permission: string): boolean {
  if (!patterns || patterns.length === 0 || !permission) return false
  return patterns.some((pattern) => patternMatches(pattern, permission))
}

/**
 * Every permission an action needs.
 *
 * Some actions need more than one at once — saving with `publish: true` is `workflow.draft` *and*
 * `workflow.publish` — and offering the action when only one of them is held produces a refusal the
 * user cannot explain.
 */
export function holdsAll(patterns: readonly string[] | null | undefined, required: readonly string[]): boolean {
  return required.every((permission) => holdsPermission(patterns, permission))
}
