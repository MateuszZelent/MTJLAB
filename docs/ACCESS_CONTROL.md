# Local access control

Lab Control uses the authenticated operating-system login. It does not store an application
password and does not provide a role selector that could be changed by the operator.

## Roles

| Role | Measurement control | Run recipes | Edit limits/settings | Approve profile | Manage roles |
|---|---:|---:|---:|---:|---:|
| `operator` | yes | yes | no | no | no |
| `engineer` | yes | yes | yes | yes | no |
| `service` | yes | yes | yes | yes | yes |

`OUTPUT OFF`, controlled ramp-to-zero, disconnect and E-STOP are never denied by RBAC.
Audit failure still blocks ARM, OUTPUT ON and new runs, but never blocks de-energising actions.

## Initial provisioning

The generated `.config/settings.yml` is local and is not tracked by Git. Before laboratory
commissioning, an administrator must add the exact operating-system account of the first
service owner:

```yaml
access_control:
  identity_provider: operating_system
  default_roles:
  - operator
  user_roles:
    "LAB\\station-owner":
    - service
```

Use the account string displayed in the application header. Restart Lab Control after changing
role assignments. On the next start, the service owner can maintain assignments in
`Settings → Access roles`. An engineer cannot grant itself the service role.

Unassigned authenticated OS accounts receive only `default_roles` (`operator` in the packaged
template). Do not change the default to `engineer` or `service` on a shared workstation.

## Evidence and traceability

- every audit JSONL record contains the OS actor and effective roles;
- access grants and denials for safety-sensitive actions are explicit audit events;
- new HDF5 runs contain `run/operator_context_json`;
- resumed runs record the current operator in the durable `run_resumed` event;
- profile approval stores the authenticated OS account in `profile.approved_by`.

The operating-system session is the authentication boundary. Domain policy, workstation login,
screen locking and account lifecycle remain responsibilities of the laboratory IT owner.
