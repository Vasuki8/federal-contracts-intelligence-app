import {
  type DatabaseConnection,
  DummyDriver,
  type Driver,
  Kysely,
  PostgresAdapter,
  PostgresIntrospector,
  PostgresQueryCompiler,
  type QueryResult,
} from "kysely";

import type { DB } from "./types";

/** A Kysely instance that compiles Postgres SQL but never opens a connection. For tests. */
export function fakeDb(driver: Driver = new DummyDriver()): Kysely<DB> {
  return new Kysely<DB>({
    dialect: {
      createAdapter: () => new PostgresAdapter(),
      createDriver: () => driver,
      createIntrospector: (db) => new PostgresIntrospector(db),
      createQueryCompiler: () => new PostgresQueryCompiler(),
    },
  });
}

/** A driver whose every query returns `rows`. */
export function driverReturning(rows: Record<string, unknown>[]): Driver {
  const connection: DatabaseConnection = {
    executeQuery: <R>() => Promise.resolve({ rows } as QueryResult<R>),
    streamQuery: () => {
      throw new Error("streamQuery is not supported by the fake driver");
    },
  };
  return new (class extends DummyDriver {
    override acquireConnection(): Promise<DatabaseConnection> {
      return Promise.resolve(connection);
    }
  })();
}

/** A driver that cannot connect, like a database that is down. */
export function driverFailingWith(message: string): Driver {
  return new (class extends DummyDriver {
    override acquireConnection(): Promise<DatabaseConnection> {
      return Promise.reject(new Error(message));
    }
  })();
}

/** A driver that records each query's SQL and parameters, answering with `rowsFor(sql)`. */
export function recordingDriver(
  rowsFor: (sql: string) => Record<string, unknown>[] = () => [],
): { driver: Driver; queries: { sql: string; parameters: readonly unknown[] }[] } {
  const queries: { sql: string; parameters: readonly unknown[] }[] = [];
  const connection: DatabaseConnection = {
    executeQuery: <R>(query: { sql: string; parameters: readonly unknown[] }) => {
      queries.push({ sql: query.sql, parameters: query.parameters });
      return Promise.resolve({ rows: rowsFor(query.sql) } as QueryResult<R>);
    },
    streamQuery: () => {
      throw new Error("streamQuery is not supported by the fake driver");
    },
  };
  const driver = new (class extends DummyDriver {
    override acquireConnection(): Promise<DatabaseConnection> {
      return Promise.resolve(connection);
    }
  })();
  return { driver, queries };
}
