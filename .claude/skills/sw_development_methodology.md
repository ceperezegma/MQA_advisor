# Mandatory workflow to follow for each change implementation in the development workflow

## Why are we doing this?

SW development with AI is a new way of doing software development. If the AI isn't managed properly, it ends up being a
waste of time, effort, and money. This is in contradiction with the advantage of using AI for SW development.

## What's the approach?

We want to make sure that the application is developed using a SW engineering approach which include best practices and
principles for profesional SW development. We want to have the lowest complexity application possible making easier to understand
and modify it. 


## What is the workflow?

This workflow provides the main steps to follow when developing an application. Don't skip any step and follow them in
strict order except if the user tell you what steps to skig or from which step start.

It's crucial to notice that if at any step of the process, you need my input, see something that doesn't make sense, see
conflicts, or something wrong, pause and let me know to provide you what you need to continue. This is to prevent the
drift of the application development beyond its goal because it costs time and money.

All "human review and acceptance" steps happen synchronously in the current session: stop and wait for my response
before continuing. Never proceed on an assumption of approval.

If I reject an artifact at a review gate, either I will change it and let you know when done, I will tell you exactly
what to change, or I will describe what's wrong and you will propose and implement the fix. Either way, re-present the
revised artifact for approval before moving on — don't proceed past the gate until I explicitly approve.

If at any step you get an error, rety 3 times and if it still fails, stop and tell me.

1. For a new project, you will need first to create the environment, install the dependencies and development tools. You
   must ask me what tools, libraries, etc. to install to get the setup environment ready. Recommended tools to install
   to get the environment set:
    * **uv:** For package management
    * **sentry:** For debugging and app monitoring
    * **ruff:** For code linting
    * **pytest:** For testing
    * **pre-commit:** For pre-commit hooks
    * **jira:** MCP for ticket management
    * **context7:** MCP for AI documentation
    * **playwright:** MCP for end-to-end testing
    * Set `privacy mode` to `ON` in claude code to avoid any sensitive information being sent to Anthropic.
    * Set `Memories` to `ON` in claude code to claude remember the previous conversation.

2. Analyze properly and in-depth what we need to do to build the application and its design. It's important to take the
   time to understand and describe the problem to solve (the goal) and how to solve it. The more detail you can provide,
   the more likely it is that I will accept the design. We can eventually use skills
   like https://github.com/mattpocock/skills to help us understand the problem to solve better and how to solve it.
3. Document the steps to follow to build the application in a .md file
4. Human review and acceptance of the .md file. Present the full document (not just a summary) and wait for approval.
5. Create the tickets in jira or issue on github accordingly to implement the application. The tickets/issues should be
   in the correct order to build the application and also include:
    * Description or goal aligned with the md doc in 2. If not aligned, align them. Be flexible to accept that the .md
      file can be wrong or incomplete and eventually we will need to be update it.
    * Description includes acceptance criteria
    * (If requested) Description includes logging details
    * Description includes testing scenarios
6. 🚨Human review and acceptance of the tickets/issues. Present the ticket links/content (title, description, acceptance
   criteria, logging, testing scenarios) and wait for approval.
7. Create a git branch for each ticket or issue to implement. See whether more than once ticket can be implemented at the same time. If this
   is the case, create a new branch for each ticket using `git worktree`, and run each worktree in its own separate
   Claude Code session. Each session follows this entire workflow independently for its own ticket, including its own
   human review gates, which I will approve in whatever order they come up across sessions.
8. Create a new spec change and make sure all the spec documents are aligned with the ticket description. Run
   `/opsx:propose` to generate the spec change. The spec change is a markdown file in the `spec` folder. If implementing
   the ticket reveals that the design doc from step 3 is wrong or incomplete, update it too and flag the discrepancy
   when presenting the spec change.
9. Make sure the spec change documents cover all what's needed to implement the ticket.
10. Check the spec change is valid by running in the terminal `openspec validate {proposal name} --strict`
11. 🚨Human review and acceptance of the spec change. Present the spec change files (proposal, design, specs) or a
    link/diff, and wait for approval.
12. Commit the spec change and push to the branch.
13. Change the ticket/issue to IN PROGRESS and implement the spec change. Run `/opsx:apply` to generate the code. If
    while implementing you discover the spec or ticket is wrong, incomplete, or ambiguous, stop immediately — don't code
    around the gap. Draft the correction to the spec change (and flag if the Jira ticket also needs updating to stay
    aligned), present the diff, and wait for approval before resuming implementation.
14. Run `uv run ruff check` and `uv run ruff format` over the modified files. This is an early check — the pre-commit
    hook will also run these on commit — so it just catches issues sooner.
15. Run the full automated test suite (`uv run pytest`) to confirm no regressions elsewhere. Don't let tests touch
    business_ideas.xlsx.
16. Run the applications and check the application is working as expected as per the acceptance criteria
17. Commit the implementation code
18. Run `/code-review`. Ask if you should run `/security-review` and wait for confirmation before run it. Present results together.
    If issues are found, make a proposal about how to fix them and their priority to fix them. Wait for user decision before moving forward.
    If you fix issues, implement them and push changes.
19. Create a git PR request to merge the branch to master. Display the  PR link/diff; wait for approval.
20. 🚨Wait for human review and acceptance of the PR request.
21. Merge the branch to master and delete the branch (es). When merging in parallel, merge PRs into master one at a
    time. After merging a branch, any other active session should pull/rebase master into its own branch before its own
    PR is merged, to catch overlapping-file conflicts.
22. Archive the spec change. Run `/opsx:archive` to generate the archive, and commit it directly (this is bookkeeping,
    not part of the reviewed implementation).
23. Change the ticket status to DONE
24. Remove this ticket's branch or worktree (`git worktree remove`) now that it's merged.
25. Clear the claude code cache using `/clear`
26. Move to the next ticket and do this until all tickets are implemented. In the parallel case, ticket-to-worktree is
    1:1 — start a new session for the next ticket rather than looping within this one.

## Commits
For commits we use the conventional commits framework as described in https://www.conventionalcommits.org/en/v1.0.0/