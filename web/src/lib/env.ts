type Env = Readonly<Record<string, string | undefined>>;

/** Read a required environment variable, failing with a message that says how to fix it. */
export function requireEnv(name: string, env: Env = process.env): string {
  const value = env[name];
  if (value === undefined || value === "") {
    throw new Error(`${name} is not set. Add it to .env (see .env.example) and try again.`);
  }
  return value;
}
