const { askForAccessibilityAccess, getAuthStatus } = require('node-mac-permissions');

const status = getAuthStatus('accessibility');
console.log('Accessibility status:', status);

if (status !== 'authorized') {
  askForAccessibilityAccess();
  console.log('Prompted for access — grant it in System Settings, then re-run this script.');
} else {
  console.log('Accessibility already granted. Ready to proceed.');
}
