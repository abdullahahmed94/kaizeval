import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { system } from "../api";
import { useSession } from "../auth";
import { ErrorNote, Field, Loading } from "../components/ui";
import { useAction, useLoad } from "../hooks";

function SignInForm() {
  const session = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      const result = await system.login(email.trim(), password);
      await session.signIn(result.token);
    });
  };

  return (
    <form onSubmit={submit} className="form login-form" aria-labelledby="signin-title">
      <h2 id="signin-title">Sign in</h2>
      <Field label="Email">
        <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" required />
      </Field>
      <Field label="Password">
        <input type="password" value={password} onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password" required />
      </Field>
      <ErrorNote error={action.error} />
      <button type="submit" className="btn btn-primary" disabled={action.busy}>
        {action.busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

function SignUpForm() {
  const session = useSession();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      const result = await system.signup(email.trim(), password, name.trim());
      await session.signIn(result.token);
    });
  };

  return (
    <form onSubmit={submit} className="form login-form" aria-labelledby="signup-title">
      <h2 id="signup-title">Create an account</h2>
      <Field label="Name">
        <input type="text" value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" maxLength={200} />
      </Field>
      <Field label="Email">
        <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" required />
      </Field>
      <Field label="Password" hint="At least 8 characters.">
        <input type="password" value={password} onChange={(event) => setPassword(event.target.value)}
          autoComplete="new-password" minLength={8} maxLength={200} required />
      </Field>
      <ErrorNote error={action.error} />
      <button type="submit" className="btn" disabled={action.busy}>
        {action.busy ? "Creating account…" : "Create account"}
      </button>
    </form>
  );
}

export function LoginPage() {
  const session = useSession();
  const location = useLocation();
  const info = useLoad(() => system.info(), []);

  if (session.user) {
    const from = (location.state as { from?: string } | null)?.from;
    return <Navigate to={from && from !== "/login" ? from : "/"} replace />;
  }

  const signupOpen = info.data?.signup_open === true;

  return (
    <main className="login">
      <div className="login-card">
        <h1 className="brand">Kaizeval</h1>
        <p className="muted">Evaluate, diagnose and safely optimize AI agents on your own infrastructure.</p>
        <ErrorNote error={session.error} />
        <div className={signupOpen ? "login-forms two" : "login-forms"}>
          <SignInForm />
          {signupOpen && <SignUpForm />}
        </div>
        {info.loading && <Loading label="Contacting the server…" />}
        <ErrorNote error={info.error} />
        {info.data && !signupOpen && (
          <p className="muted small">Self-service sign-up is disabled on this instance. Ask an administrator for an account.</p>
        )}
        {info.data && (
          <p className="muted small">
            {info.data.name} {info.data.version}, {info.data.edition} edition.
            {info.data.instance_offline ? " This instance runs in strict offline mode." : ""}
          </p>
        )}
      </div>
    </main>
  );
}
