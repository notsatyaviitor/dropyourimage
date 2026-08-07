import { Fragment } from 'react'
import { STEPS, type StepIndex } from './steps'

/**
 * Four-step progress header, ported from the design prototype.
 *
 * Steps are clickable *backwards* only. Jumping forward past the upload step would mean showing a
 * results page with no job behind it, which is exactly the kind of empty-but-confident screen the
 * prototype's fake processing produced.
 */

export function WizardHeader({
  step,
  maxReached,
  onGoTo,
}: {
  step: StepIndex
  maxReached: StepIndex
  onGoTo: (n: StepIndex) => void
}) {
  return (
    <header className="wizard-header">
      <div className="wizard-steps">
        {/* Flat siblings on purpose: `.wizard-steps` is a flex row and the ported CSS expects
            `.step-wrapper` and `.step-line` to be direct children. Wrapping each pair in a div
            would nest them one level too deep and collapse the connector lines. */}
        {STEPS.map((label, i) => {
          const n = (i + 1) as StepIndex
          const active = n <= step
          const reachable = n <= maxReached && n !== step
          return (
            <Fragment key={label}>
              {i > 0 && <div className={`step-line${n > step ? ' off' : ''}`} />}
              <div className="step-wrapper">
                <button
                  type="button"
                  className={`step-circle${active ? '' : ' off'}${reachable ? ' step-clickable' : ''}`}
                  onClick={() => reachable && onGoTo(n)}
                  disabled={!reachable}
                  aria-current={n === step ? 'step' : undefined}
                >
                  {n}
                </button>
                <div className={`step-label${active ? '' : ' off'}`}>{label}</div>
              </div>
            </Fragment>
          )
        })}
      </div>

      <button type="button" className="btn-logout" disabled title="No auth in this POC">
        Log out
        <svg width="17" height="17" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M8 3H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h4" />
          <polyline points="13,7 17,10 13,13" />
          <line x1="17" y1="10" x2="7" y2="10" />
        </svg>
      </button>
    </header>
  )
}
