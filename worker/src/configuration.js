/**
 * The configuration's text: JSON ({"chains": {...}}, as the Python relay's). Kept out of the
 * bundle (make worker-build), it's uploaded beside it: deploy.ts uploads the deployed
 * configuration as this module, and workerd runs the tests with this one, the chain the root
 * lists with the payee the tests configure (anvil's account 8); the tests configure each
 * chain through the hooks.
 */
export default '{"chains": {"31337": {"payee": "0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f"}}}';
