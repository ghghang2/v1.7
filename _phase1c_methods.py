    def _is_parallel_readable(self, tc) -> bool:
        """True if this tool call may run in a parallel read-batch.

        Only a ``run_command`` with no ``cwd`` whose command the idempotent
        read cache's classifier certifies as a *read* is eligible.  Anything
        else - a write, a browser/test run, ``delegate_task``, a ``cwd``-scoped
        read, or a command the classifier did not recognise - is left to the
        serial path so mutations and ordering are never disturbed (Phase 1c).
        """
        try:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
        except Exception:
            return False
        if name != "run_command" or args.get("cwd"):
            return False
        try:
            return executor.is_readable_command(args.get("command"))
        except Exception:
            return False

    def _batch_tool_results(self, tool_calls) -> list:
        """Execute a turn's tool calls, batching maximal runs of consecutive
        read-only calls in parallel (Phase 1c) while every other call runs
        serially.

        Returns a list of (tc, tool_name, tool_args, raw_result, had_error)
        in the ORIGINAL tool-call order - identical to what a serial
        ``for tc in tool_calls`` loop would have produced.  Reads in a batch
        are dispatched together on a small pool (so their wall-clock is the
        slowest one, not their sum) but their *results* are still returned in
        call order.  Only consecutive reads form a batch: a single non-read
        call between two reads breaks the run, so no mutation can be observed
        out of order.
        """
        results: list = []
        n = len(tool_calls)
        i = 0
        while i < n:
            if self._is_parallel_readable(tool_calls[i]):
                j = i
                while j < n and self._is_parallel_readable(tool_calls[j]):
                    j += 1
                if j - i >= 2:
                    batch = tool_calls[i:j]
                    with ThreadPoolExecutor(
                            max_workers=min(len(batch), 4)) as pool:
                        futs = [
                            pool.submit(
                                contextvars.copy_context().run,
                                executor.run_tool,
                                tc["function"]["name"],
                                tc["function"]["arguments"],
                            )
                            for tc in batch
                        ]
                    for tc, fut in zip(batch, futs):
                        try:
                            raw_result = str(fut.result())
                        except Exception as e:  # defensive: pool already ran
                            raw_result = (
                                "Tool '%s' failed: %s"
                                % (tc["function"]["name"], e)
                            )
                        results.append((
                            tc,
                            tc["function"]["name"],
                            tc["function"]["arguments"],
                            raw_result,
                            is_tool_error(tc["function"]["name"],
                                          raw_result),
                        ))
                else:
                    # A lone read: run inline (no pool) for the same result.
                    tc = tool_calls[i]
                    raw_result = executor.run_tool(
                        tc["function"]["name"],
                        tc["function"]["arguments"],
                    )
                    results.append((
                        tc,
                        tc["function"]["name"],
                        tc["function"]["arguments"],
                        raw_result,
                        is_tool_error(tc["function"]["name"], raw_result),
                    ))
                i = j
            else:
                tc = tool_calls[i]
                raw_result = executor.run_tool(
                    tc["function"]["name"],
                    tc["function"]["arguments"],
                )
                results.append((
                    tc,
                    tc["function"]["name"],
                    tc["function"]["arguments"],
                    raw_result,
                    is_tool_error(tc["function"]["name"], raw_result),
                ))
                i += 1
        return results

