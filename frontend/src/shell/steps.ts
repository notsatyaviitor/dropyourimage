/**
 * Wizard step definitions, kept out of the component file so it exports only components — React
 * Fast Refresh cannot preserve state across edits in a module that mixes the two.
 */

export const STEPS = ['Specification', 'Upload method', 'Upload', 'Complete'] as const

export type StepIndex = 1 | 2 | 3 | 4
