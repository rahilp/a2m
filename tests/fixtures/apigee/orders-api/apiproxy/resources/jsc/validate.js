var verb = context.getVariable("request.verb");
var tenant = context.getVariable("request.header.x-tenant");
context.setVariable("order.valid", String(verb === "POST" && tenant !== null));
