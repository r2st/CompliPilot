import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { ApiError } from "./api";
import { useApi } from "./use-api";

describe("useApi", () => {
  it("starts loading and resolves with the fetched data", async () => {
    const fn = vi.fn().mockResolvedValue({ hello: "world" });
    const { result } = renderHook(() => useApi(fn));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.data).toEqual({ hello: "world" });
    expect(result.current.error).toBeNull();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("surfaces an ApiError's message", async () => {
    const fn = vi.fn().mockRejectedValue(new ApiError(404, "not_found", "No such filing"));
    const { result } = renderHook(() => useApi(fn));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBe("No such filing");
    expect(result.current.data).toBeNull();
  });

  it("falls back to a generic message for a non-ApiError failure", async () => {
    const fn = vi.fn().mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useApi(fn));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBe("Something went wrong");
  });

  it("refetches when a dependency changes", async () => {
    const fn = vi.fn().mockResolvedValueOnce("first").mockResolvedValueOnce("second");
    const { result, rerender } = renderHook(({ dep }) => useApi(fn, [dep]), {
      initialProps: { dep: 1 },
    });

    await waitFor(() => expect(result.current.data).toBe("first"));

    rerender({ dep: 2 });

    await waitFor(() => expect(result.current.data).toBe("second"));
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("reload() triggers another fetch without a dependency change", async () => {
    const fn = vi.fn().mockResolvedValueOnce("first").mockResolvedValueOnce("second");
    const { result } = renderHook(() => useApi(fn));

    await waitFor(() => expect(result.current.data).toBe("first"));

    act(() => result.current.reload());

    await waitFor(() => expect(result.current.data).toBe("second"));
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("clears a previous error once a reload succeeds", async () => {
    const fn = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(500, "internal_error", "Boom"))
      .mockResolvedValueOnce("recovered");
    const { result } = renderHook(() => useApi(fn));

    await waitFor(() => expect(result.current.error).toBe("Boom"));

    act(() => result.current.reload());

    await waitFor(() => expect(result.current.data).toBe("recovered"));
    expect(result.current.error).toBeNull();
  });
});
