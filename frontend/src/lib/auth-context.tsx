"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { tokenStore } from "./api";
import { authApi } from "./resources";
import type { SessionContextResponse, TokenResponse, UserResponse } from "./types";

interface AuthContextValue {
  loading: boolean;
  isAuthenticated: boolean;
  user: UserResponse | null;
  session: SessionContextResponse | null;
  setSession: (tokens: TokenResponse) => void;
  refreshSession: () => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function cachedUser(): UserResponse | null {
  const raw = tokenStore.getUserRaw();
  if (!raw) return null;
  try {
    return JSON.parse(raw) as UserResponse;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<UserResponse | null>(cachedUser);
  const [session, setSessionState] = useState<SessionContextResponse | null>(null);

  const refreshSession = useCallback(async () => {
    if (!tokenStore.getAccess()) {
      setUser(null);
      setSessionState(null);
      return;
    }
    try {
      const ctx = await authApi.me();
      setSessionState(ctx);
      setUser(ctx.user);
    } catch {
      tokenStore.clear();
      setUser(null);
      setSessionState(null);
    }
  }, []);

  useEffect(() => {
    // Resolves the session for whatever token was cached at load.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refreshSession().finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setSession = useCallback((tokens: TokenResponse) => {
    tokenStore.setSession(tokens);
    setUser(tokens.user);
    refreshSession();
  }, [refreshSession]);

  const logout = useCallback(() => {
    tokenStore.clear();
    setUser(null);
    setSessionState(null);
    // A full reload clears any in-memory state that assumed a signed-in user.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    if (typeof window !== "undefined") window.location.href = "/login";
  }, []);

  const value = useMemo(
    () => ({
      loading,
      isAuthenticated: !!user,
      user,
      session,
      setSession,
      refreshSession,
      logout,
    }),
    [loading, user, session, setSession, refreshSession, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
