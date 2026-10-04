// Give every request a correlation id the backend and the logs can share.
var id = 'c-' + Math.floor(Math.random() * 1000000000).toString(16);
context.setVariable('request.header.X-Correlation-Id', id);
context.setVariable('corr.id', id);
