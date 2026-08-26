import type { PolicyEvent, RunMode, Step } from '../types';

export function StepItem({
  step,
  mode = 'explore',
  flags = [],
}: {
  step: Step;
  mode?: RunMode;
  flags?: PolicyEvent[];
}) {
  const hits = flags.filter((f) => f.at_step === step.index);

  return (
    <li className="step" data-mode={mode} data-status={step.status}>
      <span className="step-idx">{step.index}</span>
      <div>
        {step.ms > 0 && <span className="step-ms">{(step.ms / 1000).toFixed(2)}s</span>}
        <div className="step-act">
          {step.action}
          {step.value && <span className="arg"> {step.value}</span>}
        </div>
        {step.reason && <p className="step-reason">{step.reason}</p>}
        {step.selector && <div className="step-sel">{step.selector.primary}</div>}
        {hits.map((flag, i) => (
          <div className="flag" key={i}>
            <b>{flag.kind === 'injection' ? 'Prompt injection' : 'Safety policy'}</b>
            {flag.detail}
          </div>
        ))}
      </div>
    </li>
  );
}
