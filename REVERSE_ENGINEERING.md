# Subscription tokenomics: reconstructed harness and calculations

Source: `Tokenomics.md` (October 5, 2026) and the 20 local WebP figures. This is a reconstruction of the published method, not recovered source code or raw measurements. The folder contains no harness, API logs, meter time series, or spreadsheet.

## What the harness measures

The basic observation is the provider's billed token counts for one request and the change in every subscription usage meter after that request. For each provider, account, plan, model, and test date, the harness runs four workloads: ordinary input, cache write where supported, cache read, and output. It estimates a **meter debit per million tokens** for each token type on each meter, such as the five-hour meter, all-model weekly meter, and a model-specific weekly meter. These are subscription credit prices, not API prices.

The experiment design is explicit in `0f26243f-cdd4-41a3-b89c-6b78dac4317b_2048x1034.webp`:

| Experiment | Request design | Dominant billed tokens |
| --- | --- | --- |
| Input | Independent, single-turn calls containing a portion of *War and Peace*, a fresh random tag at the front, and a short answer (at most about 4,000 tokens). No cache marker. | Fresh input |
| Cache write | Same long prompt, explicitly marked for caching, with a fresh tag for each call. Skipped on OpenAI because the figure says it does not bill cache writes separately. | Cache writes |
| Cache read | Same marked prompt with a **fixed** tag. First call warms the cache; repeats should hit it. | Cache reads after warmup |
| Output | Short prompt (about 100 tokens) requesting a long technical essay, with responses up to about 16,000 tokens. | Output |

These are independent single-turn calls, not turns in a shared conversation. The provider's own usage metadata identifies tokens by type; prompt lengths are only a way to make the experiment informative. The harness must keep incidental tokens of the other types rather than pretending they are zero.

### Likely minimal records and loop

The article says it reads **every** displayed meter after **every** call and stores billed tokens of each type. A reproducible implementation would need at least:

```text
run: provider, account_id, plan, model, date, experiment,
     prompt_hash, cache_marker, cache_tag_policy, promo/cohort,
     meter_names, meter_reset_times
call: sequence, timestamp, provider_request_id, prompt_tag,
      billed_fresh_input, billed_cache_write, billed_cache_read,
      billed_output, meter_readings_after_call
```

```text
for each (account, plan, model, experiment):
    construct the appropriate prompt and cache policy
    repeat sequentially:
        issue one request; parse all four billed token counts
        read all visible meters; append the call record
        for each meter, detect upward crossings and save cumulative tokens
        stop when the meter-rate uncertainty is within 5%
    reject intervals that cross a reset, promotion, model change, or other usage
    estimate the dominant token rate after subtracting incidental-token debits
```

The stopping criterion and incidental-token subtraction come from the text, but the request protocol, meter-reading mechanism, retry policy, confidence calculation, and data schema are **not published**. The pseudocode above is a plausible implementation, not a claim about their actual code. Three accounts on one plan exposed a roughly 20% lower-limit A/B cohort, so account and cohort metadata matter.

## Reconstructing a rate from a quantized meter

Let `x[i,j]` be the billed millions of tokens of type `j` on call `i`, and `u[i,k]` be the post-call reading of meter `k` in percentage points. Let `b[k,j]` denote percentage points consumed per million tokens. Away from resets, the latent meter debit is approximately

```text
delta_u[k] = sum_j b[k,j] * sum_i x[i,j].
```

The displayed meter is quantized, often to whole percent. A single call may cause a real debit while the displayed value stays unchanged. The published method stores cumulative token totals at each visible increase, discards the partial interval before the first increase and after the last, and divides the **total displayed increase across complete steps** by the tokens used in those steps. If a visible increase jumps more than one meter unit, its full jump must be counted.

For a dominant token type `d`, an idealized corrected estimate over the retained calls is

```text
b[k,d] = (Delta_u[k] - sum_(j != d) b[k,j] * X[j]) / X[d],
where X[j] is the billed MTok of type j over those calls.
```

The correction is required because, for example, every input-heavy call still produces output. One could jointly solve the four experiment equations as a small linear system; the article only says it subtracts prices already measured for incidental types. It does not specify the order or solver.

The `07a6b9ad-a758-4e40-9894-595aaf3d0e08_2048x1143.webp` illustration uses 8,000-token calls and a true rate of 21,000 tokens per 1%. Eight complete visible steps average to about that rate even though individual steps appear to take 16,000 or 24,000 tokens. With equal-sized calls, if `m` complete one-point steps span `D` observed tokens and a call contains `q` tokens, a conservative boundary bracket is roughly `m/(D+q)` to `m/(D-q)` percent per token. The actual bracket should use the token counts of the two endpoint calls. The article says it continues until its range is within ±5%; it does not give the precise range formula or treatment of asynchronous meter updates.

For a cache-read experiment, the initial cache-writing call is warmup and should not be treated as a cache-read sample. The text says that if 500 million cache-read tokens do not move a meter, it labels cache reads free. That is a **detection rule**, not proof of a zero price: a nonzero debit can remain below a quantized meter step.

## Window limits and monthly tokens

If a token type consumes `b` **percentage points per MTok** on a meter, that meter holds `100/b` MTok of that type. For a mixed workload with token shares `w[j]`, the meter's blended debit is `sum_j w[j]*b[k,j]`; its capacity is `100 / sum_j w[j]*b[k,j]` MTok. Compute this separately for every shared and model-specific meter, then apply the meter that constrains that workload. The five-hour limit can also constrain a burst even when the weekly limit determines the theoretical monthly maximum.

The worked Fable 5.1 input example in `51471699-71b4-4d96-96ef-5bcc29bed2b0_2048x901.webp` gives:

| Meter | Debit for 1M fresh input | Implied window capacity |
| --- | ---: | ---: |
| Five-hour | 3.3% | about 30M input tokens |
| All-model weekly | 0.88% | about 113M input tokens |
| Fable-specific weekly | 1.76% | about 57M input tokens |

The Fable-specific weekly meter binds: `57M * 4.345 weeks/month ≈ 247M` fresh input tokens per month. The five-hour meter allows enough separate windows to reach this weekly cap in principle. This is an **ideal fully used** limit, not a prediction of ordinary user consumption. The actual attainable amount depends on timing, concurrency, resets, and workload shape.

For a single fixed mix, the general theoretical monthly calculation is approximately `min_k [(100 / sum_j w[j]*b[k,j]) * resets_per_month[k]]`, after accounting for any separate model allocation. Whether SemiAnalysis implements the blend before taking the minimum is not stated. Blending individually selected per-token bottlenecks first can give a different answer if different meters bind for different token types.

## Subscription dollars, API value, and a reproduced example

For monthly fee `F`, define the monthly capacity of type `j` alone as `C[j]` MTok. Its implicit subscription price is `s[j] = F/C[j]` dollars per MTok. Equivalently, if 1 MTok consumes `p[j]` percent of the monthly capacity, `s[j] = F*p[j]/100`. Let `a[j]` be public API dollars per MTok and `w[j]` the chosen token mix. Then:

```text
subscription_blended_price = sum_j w[j] * s[j]
API_blended_price          = sum_j w[j] * a[j]
monthly_total_MTok         = F / subscription_blended_price
API_equivalent_value       = monthly_total_MTok * API_blended_price
value_multiple             = API_equivalent_value / F
```

The example in `a979d2fd-77fa-456d-b1c8-efb9f82d174a_2048x1002.webp` is Claude Max 20x at $200/month using Opus 5.5. Its chart displays subscription prices of about `$0.0893`, `$0.1117`, `$0.0022`, and `$0.4463` per MTok for fresh input, cache write, cache read, and output. API prices are `$4`, `$5`, `$0.20`, and `$20` per MTok. The shown workload mix (0.4%, 2.6%, 96.6%, 0.3%) is rounded and sums to 99.9%. The API contributions in the figure imply hidden shares of about **0.43%, 2.63%, 96.60%, and 0.34%**, summing to 100%. These produce `$0.4099/MTok` at API rates. With hidden precision in the subscription prices, the chart uses about `$0.0069915/MTok`: `$200 / $0.0069915 ≈ 28,606 MTok` (28.6B tokens), then `28,606 * $0.4099 ≈ $11,726` of API-equivalent value. Recalculating from the displayed rounded inputs alone does **not** reproduce the last digits.

This value is the cost of buying those tokens at list API prices, **not** Anthropic revenue, actual cash cost, or a guaranteed amount a subscriber will use. It varies with the token mix and API price schedule. The same plan has a very different value for a different model or workload.

The headline mid-tier comparison in `44bfcff4-5589-4822-a348-bc4e83cf478b_2048x1161.webp` and `e6b9e23d-8049-4235-8c21-9a5a2b0cd1f0_2048x1153.webp` can also be decomposed. On the $200 plans, Opus 5.5 is shown at `$11,726` and `28.6B` tokens, versus GPT-6.1 Sol at `$2,084` and `10.2B` tokens. The API-value ratio is `5.63x`, but the token-volume ratio is `2.80x`. The rest of the value ratio comes from Opus's roughly `2x` higher blended API list price under this workload. The “5x+” claim is therefore a model-and-workload comparison, not a universal measure of plan utility or model quality.

### Fable cap and footnote ambiguity

The Fable-specific weekly limit is roughly half its shared weekly limit in the worked figure. The prose says a Max 20x user can consume **$2,485 of Fable 5.1 API value and still have about half the shared limit left**. This indicates that the `$2,485` chart value is the attainable Fable-specific allocation, not twice that amount. Some chart footnotes describe API value as the plan's “full monthly usage limit”; that wording is imprecise for Fable because a separate model cap binds. The margin table's Fable row is also consistent with treating `$2,485` as its actual cap.

## Margin and compute calculations

The margin tables assume the underlying compute cost equals `8%` of API list revenue (92% API gross margin). At fraction `f` of measured subscription capacity consumed, they imply

```text
subscription_gross_margin(f) = 1 - [f * API_equivalent_value * 0.08] / monthly_fee
break_even_utilization      = monthly_fee / [API_equivalent_value * 0.08].
```

For Opus 5.5 on Max 20x: at 100% use, `1 - 0.08*11726/200 = -369%`; at 20% use, about `6%`; break-even is about `21%`. For Fable 5.1 on the same plan: at 100% of its model-specific allocation, `1 - 0.08*2485/200 ≈ 1%`; at 20%, about `80%`. This is a **scenario calculation** using a uniform 92% API margin, not observed plan cost accounting.

The opening `2ac5c578-4092-4afe-bdaf-cbf61f7f83b5_2048x1194.webp` rough Anthropic graphic is internally consistent. Given 10% of revenue from subscriptions, subscription revenue of `$16M/MW`, and API revenue of `$102.9M/MW`, subscription compute share is `(0.10/16) / (0.10/16 + 0.90/102.9) = 41.7%`. Blended revenue is `1/(0.10/16 + 0.90/102.9) = $66.7M/MW`; at `$8M/MW` compute COGS, blended gross margin is `88.0%`.

## What cannot be recovered from these files

- The exact request payloads, *War and Peace* excerpt, essay prompt, cache parameters, model versions, metering endpoint or CLI commands.
- Raw billed-token/meter observations, estimator implementation, retries, meter latency handling, reset handling, and treatment of call failures.
- Unrounded workload shares, subscription rates, API pricing snapshots, and all data behind the dashboard or comparison charts.
- Whether their published monthly capacity formula accounts for calendar boundary effects and different limiting meters under a mixed workload.

The published comparisons are a dated snapshot, and the paper itself says plans can change silently. The methodology can be reproduced from this report; the exact published rates cannot be independently verified from the supplied files alone.
