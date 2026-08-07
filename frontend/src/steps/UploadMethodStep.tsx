/**
 * Step 2 — upload method.
 *
 * The prototype offered Manual and SFTP as equal choices, with SFTP revealing host/username/password
 * fields. There is no SFTP ingest in this backend — `POST /jobs` takes a multipart zip and nothing
 * else. So the card stays, to show the shape of the full product, but it is inert and says why.
 *
 * The credential inputs are deliberately gone rather than disabled: a password field that goes
 * nowhere invites someone to type a real password into a demo.
 */

export function UploadMethodStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  return (
    <div className="wizard-page active">
      <div className="page-title-row">
        <div className="ptrow-left">
          <button type="button" className="btn-back" onClick={onBack}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <polyline points="10,3 5,8 10,13" />
            </svg>
            Back
          </button>
          <h1 className="page-title">Select upload method</h1>
        </div>
        <button type="button" className="btn-next" onClick={onNext}>
          Next
        </button>
      </div>

      <div className="method-cards-row">
        <div className="method-card selected">
          <div className="method-icon-wrap">
            <svg viewBox="0 0 28 28" fill="none" stroke="#1C2B3C" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 20V10M10 14l4-4 4 4" />
              <path d="M5 20.5A5 5 0 015.5 11a6 6 0 0111.6-1.5A4 4 0 0123 14h1a3 3 0 010 6H6" />
            </svg>
          </div>
          <div className="method-title">Manual Upload</div>
          <div className="method-desc">
            Upload images from your computer. Drag-and-drop or pick multiple files; they are packaged
            into one batch and sent to the processing API.
          </div>
          <div className="method-radio">
            <div className="method-radio-dot" /> Selected
          </div>
        </div>

        <div className="method-card method-card-inert" aria-disabled="true">
          <div className="method-icon-wrap">
            <svg viewBox="0 0 28 28" fill="none" stroke="#1C2B3C" strokeWidth="2" strokeLinecap="round">
              <rect x="3" y="4" width="22" height="8" rx="1.5" />
              <rect x="3" y="16" width="22" height="8" rx="1.5" />
              <circle cx="22.5" cy="8" r="1.5" fill="#1C2B3C" stroke="none" />
              <circle cx="22.5" cy="20" r="1.5" fill="#1C2B3C" stroke="none" />
            </svg>
          </div>
          <div className="method-title">SFTP Upload</div>
          <div className="method-desc">
            Pull images from your own server for bulk and automated workflows.
          </div>
          <div className="method-poc-banner">
            Not built in this POC. The API accepts a single multipart upload only — there is no SFTP
            ingest behind this card, so its credential fields have been left out rather than shown
            going nowhere.
          </div>
        </div>
      </div>
    </div>
  )
}
