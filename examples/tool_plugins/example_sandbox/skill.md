# 🧪 Example sandbox skill

Use `example_sandbox` when a goal needs a tool that must run its own
subprocess or use the network. The module executes in the bubblewrap
sandbox, not in Aidon's process, so those operations are contained.
Adoption still needs the owner's approval, and the card names the
declared `network` capability first.
