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

      {/*
        No "Log out". There is no authentication in this application — the deployment puts a gate in
        front of it instead (see docs/DEPLOY.md) — so the control had nothing to log out of and was
        rendered disabled. A dead control implies a feature that exists somewhere, which invites the
        question "why can't I sign out?" about a session that was never created.
      */}
    </header>
  )
}
