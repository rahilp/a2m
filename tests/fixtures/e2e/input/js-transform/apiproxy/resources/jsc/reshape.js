// Reshape the caller's body into the id the backend expects.
var body = JSON.parse(context.getVariable('request.content') || '{}');
context.setVariable('reshaped.id', String(body.id || ''));
