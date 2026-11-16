# Stepback

A time-travel debugger for AI agents. *(Full README coming with the first release.)*

```python
import stepback
client = stepback.wrap(Anthropic())
```

```bash
stepback ls                      # list recorded runs
stepback show <run-id-or-name>   # step tree with tokens, cost and latency
stepback show <run> --step 2     # one step's full messages
stepback prices                  # the price table used for cost estimates
```

Configuration, including custom prices in `.stepback/prices.toml`: see
[docs/configuration.md](docs/configuration.md).
